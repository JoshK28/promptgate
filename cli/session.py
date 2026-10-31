"""CLI authentication and HTTP requests. Importing makes no AWS calls."""

import json as json_module
import os
from pathlib import Path

import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
import httpx


class SessionError(Exception):
    """A safe message for the CLI; never include tokens or raw exceptions."""


class Session:
    def __init__(
        self, base_url, client_id, region,
        token_path="~/.promptgate/tokens.json", transport=None, login_fn=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.region = region
        self.token_path = Path(token_path).expanduser()
        self.transport = transport
        self.login_fn = login_fn
        # CI must neither reuse another user's saved tokens nor write its own.
        self._persist_tokens = not bool(os.environ.get("PROMPTGATE_USERNAME"))
        self.tokens = {}
        if self._persist_tokens:
            self._load_tokens()

    def _load_tokens(self):
        try:
            tokens = json_module.loads(self.token_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            raise SessionError("Cannot read saved tokens; sign in again.") from None
        if not isinstance(tokens, dict) or not isinstance(tokens.get("IdToken"), str):
            raise SessionError("Invalid saved tokens; sign in again.")
        self.tokens = tokens

    def _save_tokens(self):
        if not self._persist_tokens:
            return
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Restrict newly created files on systems that support POSIX modes.
            descriptor = os.open(
                self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                json_module.dump(self.tokens, token_file)
        except OSError:
            raise SessionError("Cannot save sign-in tokens.") from None

    def _authenticate(self, flow, parameters):
        try:
            initiate_auth = self.login_fn
            if initiate_auth is None:
                # AWS when run by Josh: a public client needs no AWS credentials.
                client = boto3.client(
                    "cognito-idp", region_name=self.region,
                    config=Config(signature_version=botocore.UNSIGNED),
                )
                initiate_auth = client.initiate_auth
            response = initiate_auth(
                ClientId=self.client_id, AuthFlow=flow, AuthParameters=parameters,
            )
        except (BotoCoreError, ClientError):
            raise SessionError("Cognito sign-in failed; check your sign-in settings.") from None
        if "ChallengeName" in response:
            raise SessionError("finish sign-in in the dashboard first")
        tokens = response.get("AuthenticationResult")
        if not isinstance(tokens, dict) or not tokens.get("IdToken"):
            raise SessionError("Cognito did not return an ID token.")
        return tokens

    def login(self, username, password):
        self.tokens = self._authenticate("USER_PASSWORD_AUTH", {
            "USERNAME": username, "PASSWORD": password,
        })
        self._save_tokens()

    def _refresh(self):
        refresh_token = self.tokens.get("RefreshToken")
        if not refresh_token:
            raise SessionError("No refresh token; sign in again.")
        tokens = self._authenticate("REFRESH_TOKEN_AUTH", {
            "REFRESH_TOKEN": refresh_token,
        })
        # Cognito's refresh response normally omits the original refresh token.
        self.tokens = {**tokens, "RefreshToken": tokens.get("RefreshToken", refresh_token)}
        self._save_tokens()

    def request(self, method, path, json=None):
        if not self.tokens.get("IdToken"):
            raise SessionError("Sign in with login or set the CI sign-in variables first.")
        try:
            with httpx.Client(transport=self.transport) as client:
                response = client.request(
                    method, self.base_url + path, json=json,
                    headers={"Authorization": f"Bearer {self.tokens['IdToken']}"},
                )
                if response.status_code == 401:
                    self._refresh()
                    response = client.request(
                        method, self.base_url + path, json=json,
                        headers={"Authorization": f"Bearer {self.tokens['IdToken']}"},
                    )
                    if response.status_code == 401:
                        raise SessionError("Authentication failed after one refresh; sign in again.")
                # Keep other statuses (including 503) for the command to handle.
                return response
        except httpx.HTTPError:
            raise SessionError("HTTP request failed; check the API URL and connection.") from None
