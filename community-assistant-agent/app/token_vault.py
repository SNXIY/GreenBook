import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class DelegatedTokenVault:
    """Encrypt short-lived delegated JWTs at rest and erase them after a run."""

    def __init__(self, service_secret: str) -> None:
        key = base64.urlsafe_b64encode(
            hashlib.sha256(
                f"greenbook-assistant-token-v1:{service_secret}".encode()
            ).digest()
        )
        self._fernet = Fernet(key)

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, ciphertext: str | None) -> str:
        if not ciphertext:
            raise ValueError("Creator delegation token is unavailable")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Creator delegation token cannot be decrypted") from exc

