"""
Telegram Passkey (FIDO2 / WebAuthn) Service.

Enables registering cryptographic passkeys (ECDSA P-256 / ES256) on active Telegram sessions,
and logging into Telegram accounts using these passkeys without SMS or phone calls.
"""

import asyncio
import base64
import ctypes
import hashlib
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path

from core.storage import read_json, write_json_atomic
from core.windows_dpapi import CurrentUserDPAPI

import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pyrogram import Client, raw
from pyrogram.session import Session, Auth
from pyrogram.errors import (
    SessionPasswordNeeded,
    UserMigrate,
    PhoneMigrate,
    NetworkMigrate,
    FloodWait,
    RPCError,
)

logger = logging.getLogger(__name__)
_PASSKEY_FORMAT = "uznik-passkey-v1"
_DPAPI_ENTROPY = b"uznik-passkey-dpapi-v1"


def _zero_bytes(buf: bytearray) -> None:
    if not buf:
        return
    for idx in range(len(buf)):
        buf[idx] = 0


def _passkey_aad(account_id: str) -> bytes:
    return f"{_PASSKEY_FORMAT}|{account_id}".encode("utf-8")


def b64url(data: bytes) -> str:
    """Encode bytes to Base64URL string without padding."""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def b64url_decode(s: str) -> bytes:
    """Decode Base64URL string with optional padding."""
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)

class PasskeyService:
    def __init__(self, api_id: int, api_hash: str, passkeys_dir: str = "data/passkeys"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.passkeys_dir = passkeys_dir
        os.makedirs(self.passkeys_dir, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.passkeys_dir, 0o700)

    def _passkey_path(self, account_id: str) -> Path:
        base = Path(self.passkeys_dir).resolve()
        target = (base / f"{account_id}.json").resolve()
        if not target.is_relative_to(base) or target == base:
            raise ValueError("Invalid passkey account identifier")
        return target

    def save_passkey(self, account_id: str, record: Dict[str, Any]) -> Path:
        """Persist passkey material encrypted with Windows CurrentUser DPAPI (+ AES-256-GCM)."""
        file_path = self._passkey_path(account_id)
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if sys.platform == "win32":
            dek = bytearray(secrets.token_bytes(32))
            nonce = secrets.token_bytes(12)
            try:
                wrapped = CurrentUserDPAPI().protect(bytes(dek), _DPAPI_ENTROPY)
                ciphertext = AESGCM(bytes(dek)).encrypt(nonce, payload, _passkey_aad(account_id))
            finally:
                _zero_bytes(dek)
            envelope: Dict[str, Any] = {
                "format": _PASSKEY_FORMAT,
                "protection": CurrentUserDPAPI.name,
                "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
            write_json_atomic(file_path, envelope)
        else:
            from core.private_storage import write_private_json
            write_private_json(file_path, record)
        if os.name != "nt" and file_path.exists():
            file_path.chmod(0o600)
        return file_path

    def load_passkey(self, account_id: str) -> Dict[str, Any]:
        """Load passkey record, decrypting DPAPI envelopes and migrating legacy plaintext cleanly."""
        file_path = self._passkey_path(account_id)
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("Passkey file is malformed")
        if raw.get("format") == _PASSKEY_FORMAT:
            if raw.get("protection") != CurrentUserDPAPI.name:
                raise ValueError("Unsupported passkey protection format")
            if sys.platform != "win32":
                raise ValueError("DPAPI-protected passkey requires its original Windows user")
            wrapped = base64.b64decode(raw["wrapped_key"])
            nonce = base64.b64decode(raw["nonce"])
            ciphertext = base64.b64decode(raw["ciphertext"])
            dek = bytearray(CurrentUserDPAPI().unprotect(wrapped, _DPAPI_ENTROPY))
            try:
                plain = AESGCM(bytes(dek)).decrypt(nonce, ciphertext, _passkey_aad(account_id))
            finally:
                _zero_bytes(dek)
            data = json.loads(plain.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Decrypted passkey payload is malformed")
            return data
        if raw.get("format") == "uznik-private-json-v1":
            from core.private_storage import read_private_json
            data, _ = read_private_json(file_path)
            if not isinstance(data, dict):
                raise ValueError("Decrypted passkey payload is malformed")
            return data
        # Legacy plaintext JSON: migrate automatically when private key material is present.
        if raw.get("private_key_pem"):
            try:
                self.save_passkey(account_id, raw)
            except Exception as exc:
                logger.warning("Could not migrate legacy passkey file for %s: %s", account_id, type(exc).__name__)
        return raw

    def generate_keypair(self) -> tuple[ec.EllipticCurvePrivateKey, str]:
        """Generate a new ECDSA SECP256R1 (P-256) private key and return PEM."""
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode("utf-8")
        return private_key, pem

    def build_registration_credential(
        self,
        challenge: str,
        rp_id: str,
        private_key: ec.EllipticCurvePrivateKey
    ) -> tuple[raw.types.InputPasskeyCredentialPublicKey, str, bytes]:
        """Construct WebAuthn attestation object for Telegram account.RegisterPasskey."""
        pub_numbers = private_key.public_key().public_numbers()
        x_bytes = pub_numbers.x.to_bytes(32, "big")
        y_bytes = pub_numbers.y.to_bytes(32, "big")

        cred_id = secrets.token_bytes(32)
        cred_id_b64 = b64url(cred_id)

        client_data_dict = {
            "type": "webauthn.create",
            "challenge": challenge,
            "origin": f"https://{rp_id}",
            "crossOrigin": False
        }
        client_data_json = json.dumps(client_data_dict, separators=(",", ":"))

        # COSE Key representation for ES256 (-7)
        cose_key = {
            1: 2,    # Key type: EC2
            3: -7,   # Alg: ES256
            -1: 1,   # Curve: P-256
            -2: x_bytes,
            -3: y_bytes
        }
        cose_key_bytes = cbor2.dumps(cose_key)

        rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        flags = bytes([0x45])  # User Present (1) + User Verified (4) + Attested Credential Data (0x40)
        sign_count = (0).to_bytes(4, "big")
        aaguid = bytes(16)      # 16 zeroes
        cred_id_len = len(cred_id).to_bytes(2, "big")

        attested_cred_data = aaguid + cred_id_len + cred_id + cose_key_bytes
        auth_data = rp_id_hash + flags + sign_count + attested_cred_data

        attestation_obj = {
            "fmt": "none",
            "attStmt": {},
            "authData": auth_data
        }
        attestation_bytes = cbor2.dumps(attestation_obj)

        input_response = raw.types.InputPasskeyResponseRegister(
            client_data=raw.types.DataJSON(data=client_data_json),
            attestation_data=attestation_bytes
        )
        input_cred = raw.types.InputPasskeyCredentialPublicKey(
            id=cred_id_b64,
            raw_id=cred_id_b64,
            response=input_response
        )
        return input_cred, cred_id_b64, cred_id

    async def _invoke_with_retry(self, client: Client, request: Any, *, max_attempts: int = 3) -> Any:
        for attempt in range(max_attempts):
            try:
                return await client.invoke(request)
            except FloodWait as fw:
                seconds = int(getattr(fw, "value", getattr(fw, "x", 0)) or 0)
                if seconds > 30 or attempt + 1 >= max_attempts:
                    raise
                await asyncio.sleep(max(1, seconds) + 0.5)

    async def _switch_dc(self, client: Client, target_dc: int) -> None:
        dc_opt = await client.get_dc_option(target_dc, ipv6=client.ipv6)
        ip = dc_opt.ip_address
        port = dc_opt.port

        await client.session.stop()
        await client.storage.dc_id(target_dc)
        await client.storage.server_address(ip)
        await client.storage.port(port)

        test_mode = await client.storage.test_mode() or False
        auth_key = await Auth(client, target_dc, ip, port, test_mode).create()
        await client.storage.auth_key(auth_key)

        client.session = Session(client, target_dc, ip, port, auth_key, test_mode)
        await client.session.start()

    async def register_passkey(self, client: Client, account_id: str) -> Dict[str, Any]:
        """
        Register a new passkey on an active Telegram client and save credentials encrypted on disk.
        """
        if not client.is_connected:
            await client.connect()

        me = await client.get_me()
        dc_id = await client.storage.dc_id()

        # Step 1: Init registration
        init_res = await self._invoke_with_retry(client, raw.functions.account.InitPasskeyRegistration())
        opts = json.loads(init_res.options.data)["publicKey"]
        challenge = opts["challenge"]
        rp_id = opts["rp"]["id"]

        # Step 2: Generate keypair and build WebAuthn credential
        private_key, pem = self.generate_keypair()
        input_cred, cred_id_b64, _ = self.build_registration_credential(challenge, rp_id, private_key)

        # Step 3: Register in Telegram
        reg_res = await self._invoke_with_retry(client, raw.functions.account.RegisterPasskey(credential=input_cred))
        passkey_id = reg_res.id

        passkey_record = {
            "account_id": account_id,
            "passkey_id": passkey_id,
            "cred_id_b64": cred_id_b64,
            "user_id": me.id,
            "dc_id": dc_id,
            "phone": me.phone_number or "",
            "username": me.username or "",
            "first_name": me.first_name or "",
            "rp_id": rp_id,
            "user_handle": f"{dc_id}:{me.id}",
            "sign_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "private_key_pem": pem
        }

        self.save_passkey(account_id, passkey_record)
        logger.info("Registered passkey for %s (DC %s)", account_id, dc_id)
        return passkey_record

    def build_login_credential(
        self,
        challenge: str,
        rp_id: str,
        passkey_id: str,
        private_key_pem: str,
        user_handle: str,
        *,
        cred_id_b64: Optional[str] = None,
        sign_count: int = 1,
    ) -> raw.types.InputPasskeyCredentialPublicKey:
        """Construct WebAuthn authentication response for Telegram auth.FinishPasskeyLogin."""
        client_data_dict = {
            "type": "webauthn.get",
            "challenge": challenge,
            "origin": f"https://{rp_id}",
            "crossOrigin": False
        }
        client_data_json = json.dumps(client_data_dict, separators=(",", ":"))
        client_data_hash = hashlib.sha256(client_data_json.encode("utf-8")).digest()

        rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        flags = bytes([0x05])  # User Present (1) + User Verified (4)
        count_bytes = max(0, int(sign_count)).to_bytes(4, "big")
        auth_data = rp_id_hash + flags + count_bytes

        priv_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        signature = priv_key.sign(auth_data + client_data_hash, ec.ECDSA(hashes.SHA256()))

        login_resp = raw.types.InputPasskeyResponseLogin(
            client_data=raw.types.DataJSON(data=client_data_json),
            authenticator_data=auth_data,
            signature=signature,
            user_handle=user_handle
        )
        return raw.types.InputPasskeyCredentialPublicKey(
            id=passkey_id,
            raw_id=cred_id_b64 or passkey_id,
            response=login_resp
        )

    async def login_with_passkey(
        self,
        passkey_data: Dict[str, Any],
        two_fa_password: Optional[str] = None,
        session_name: Optional[str] = None,
        save_session_dir: Optional[str] = None,
        proxy: Optional[Dict[str, Any]] = None,
    ) -> Client:
        """
        Log into Telegram using stored passkey data.
        Bypasses SMS and phone calls.
        If 2FA is required, uses two_fa_password to complete authorization.
        """
        if not proxy:
            raise ValueError("Passkey login requires a proxy; direct connection is forbidden")
        s_name = session_name or f"passkey_{passkey_data['account_id']}"
        from modules.fingerprint_generator import FingerprintGenerator

        pk_dir = Path(self.passkeys_dir)
        fp_path = (pk_dir.parent if pk_dir.name == "passkeys" else pk_dir) / "fingerprints.json"
        fp = FingerprintGenerator(str(fp_path)).get_params(str(passkey_data["account_id"]))
        client = Client(
            name=s_name, api_id=self.api_id, api_hash=self.api_hash,
            in_memory=save_session_dir is None, workdir=save_session_dir or ".",
            no_updates=True, proxy=proxy,
            device_model=fp["device_model"],
            system_version=fp["system_version"],
            app_version=fp["app_version"],
            lang_code=fp["lang_code"],
            system_lang_code=fp.get("system_lang_code", fp["lang_code"]),
            lang_pack=fp.get("lang_pack", "android"),
        )
        try:
            return await asyncio.wait_for(
                self._login_client(client, passkey_data, two_fa_password), timeout=120,
            )
        except BaseException:
            if client.is_connected:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=15)
                except Exception:
                    logging.getLogger(__name__).warning("Could not close failed Passkey client")
            raise

    def _bump_sign_count(self, passkey_data: Dict[str, Any], next_sign_count: int) -> None:
        passkey_data["sign_count"] = next_sign_count
        account_id = passkey_data.get("account_id")
        if not account_id:
            return
        try:
            path = self._passkey_path(str(account_id))
            if path.is_file() and passkey_data.get("private_key_pem"):
                self.save_passkey(str(account_id), passkey_data)
        except Exception as exc:
            logger.debug("Could not persist passkey sign_count for %s: %s", account_id, type(exc).__name__)

    async def _login_client(
        self, client: Client, passkey_data: Dict[str, Any], two_fa_password: Optional[str],
    ) -> Client:
        target_dc = passkey_data.get("dc_id", 2)
        user_id = passkey_data["user_id"]
        user_handle = passkey_data.get("user_handle") or f"{target_dc}:{user_id}"
        passkey_id = passkey_data["passkey_id"]
        cred_id_b64 = passkey_data.get("cred_id_b64") or passkey_id
        priv_pem = passkey_data["private_key_pem"]

        await client.connect()
        curr_dc = await client.storage.dc_id()

        if curr_dc != target_dc:
            await self._switch_dc(client, target_dc)

        for attempt in range(4):
            try:
                # Step 1: InitPasskeyLogin
                init_res = await self._invoke_with_retry(
                    client, raw.functions.auth.InitPasskeyLogin(api_id=self.api_id, api_hash=self.api_hash)
                )
                opts = json.loads(init_res.options.data)["publicKey"]
                challenge = opts["challenge"]
                rp_id = opts["rpId"]

                # Step 2: Build signed credential with monotonic sign_count
                next_sign_count = int(passkey_data.get("sign_count") or 0) + 1 + attempt
                cred = self.build_login_credential(
                    challenge=challenge,
                    rp_id=rp_id,
                    passkey_id=passkey_id,
                    private_key_pem=priv_pem,
                    user_handle=user_handle,
                    cred_id_b64=cred_id_b64,
                    sign_count=next_sign_count,
                )

                # Step 3: FinishPasskeyLogin
                auth_res = await self._invoke_with_retry(
                    client, raw.functions.auth.FinishPasskeyLogin(credential=cred)
                )
                if isinstance(auth_res, raw.types.auth.Authorization):
                    await client.storage.user_id(auth_res.user.id)
                    await client.storage.is_bot(False)
                    self._bump_sign_count(passkey_data, next_sign_count)
                    return client
                break
            except (UserMigrate, PhoneMigrate, NetworkMigrate) as migrate:
                new_dc = int(getattr(migrate, "value", 0) or 0)
                if not new_dc or attempt >= 3:
                    raise
                await self._switch_dc(client, new_dc)
            except SessionPasswordNeeded:
                if not two_fa_password:
                    raise RuntimeError(f"Account {passkey_data['account_id']} requires 2FA password to finish login.")
                await client.check_password(two_fa_password)
                self._bump_sign_count(passkey_data, int(passkey_data.get("sign_count") or 0) + 1 + attempt)
                return client

        raise RuntimeError("Telegram did not return an authorized Passkey session")

    async def get_remote_passkeys(self, client: Client) -> List[raw.types.Passkey]:
        """Fetch list of passkeys registered on Telegram servers for this client."""
        if not client.is_connected:
            await client.connect()
        res = await client.invoke(raw.functions.account.GetPasskeys())
        return list(res.passkeys)

    async def delete_passkey(self, client: Client, passkey_id: str) -> None:
        """Delete a passkey from Telegram servers."""
        if not client.is_connected:
            await client.connect()
        await client.invoke(raw.functions.account.DeletePasskey(id=passkey_id))
