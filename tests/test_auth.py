"""Task 11: verify locally signed ID tokens. Local only, no AWS or JWKS fetch."""

import hashlib
import hmac
import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from api.auth import AuthError, verify_id_token


ISSUER = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_test"
CLIENT_ID = "7u4e63r5maqprgjun2o2ojr090"


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def claims(self, **overrides):
        claims = {
            "sub": "alice",
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "exp": int(time.time()) + 300,
            "token_use": "id",
        }
        claims.update(overrides)
        return claims

    def token(self, **overrides):
        return jwt.encode(
            self.claims(**overrides), self.private_key, algorithm="RS256",
            headers={"kid": "local-test-key"},
        )

    def verify(self, token):
        return verify_id_token(
            token,
            key_for=lambda _: self.public_key,
            issuer=ISSUER,
            client_id=CLIENT_ID,
        )

    def test_valid_token_returns_sub(self):
        claims = self.verify(self.token())
        self.assertEqual(claims["sub"], "alice")

    def test_wrong_audience_rejected(self):
        with self.assertRaises(AuthError):
            self.verify(self.token(aud="another-app-client"))

    def test_wrong_issuer_rejected(self):
        with self.assertRaises(AuthError):
            self.verify(self.token(iss=ISSUER + "-other-pool"))

    def test_expired_token_rejected(self):
        # 120 seconds exceeds the verifier's planned 30-second leeway.
        with self.assertRaises(AuthError):
            self.verify(self.token(exp=int(time.time()) - 120))

    def test_access_token_rejected(self):
        # Keep the valid audience so this isolates the token_use check.
        with self.assertRaises(AuthError):
            self.verify(self.token(token_use="access"))

    def test_hs256_token_rejected(self):
        # Construct the attack with stdlib HMAC: PyJWT itself refuses to sign
        # HS256 using an RSA public key, including DER bytes, as its secret.
        public_bytes = self.public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        header = jwt.utils.base64url_encode(b'{"alg":"HS256","typ":"JWT"}')
        payload = jwt.utils.base64url_encode(json.dumps(self.claims()).encode("utf-8"))
        signing_input = header + b"." + payload
        signature = hmac.new(public_bytes, signing_input, hashlib.sha256).digest()
        token = (signing_input + b"." + jwt.utils.base64url_encode(signature)).decode("ascii")

        with self.assertRaises(AuthError):
            self.verify(token)

    def test_tampered_payload_rejected(self):
        header, payload, signature = self.token().split(".")
        # Change one character of sub, preserving valid JSON and the old signature.
        edited = jwt.utils.base64url_decode(payload).replace(b'"alice"', b'"alica"')
        payload = jwt.utils.base64url_encode(edited).decode("ascii")

        with self.assertRaises(AuthError):
            self.verify(f"{header}.{payload}.{signature}")


if __name__ == "__main__":
    unittest.main()
