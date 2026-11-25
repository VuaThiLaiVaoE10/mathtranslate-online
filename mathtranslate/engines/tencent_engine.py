import os
from typing import Optional

from .. import tencent
from ..config import config
from ..tencentcloud.common import credential
from ..tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from ..tencentcloud.tmt.v20180321 import tmt_client
from .base import BaseTranslator


class TencentTranslator(BaseTranslator):
    """Tencent Cloud TMT engine wrapping mathtranslate.tencent.Translator."""

    name = "tencent"
    supports_multithread = False

    def __init__(
        self,
        secret_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "ap-shanghai",
        **_kwargs,
    ):
        resolved_id = (
            secret_id
            if secret_id is not None
            else (
                os.environ.get("TENCENT_SECRET_ID")
                or getattr(config, "tencent_secret_id", None)
            )
        )
        resolved_key = (
            secret_key
            if secret_key is not None
            else (
                os.environ.get("TENCENT_SECRET_KEY")
                or getattr(config, "tencent_secret_key", None)
            )
        )
        self.secret_id = str(resolved_id).strip() if resolved_id else None
        self.secret_key = str(resolved_key).strip() if resolved_key else None
        self.region = region

        prev_id = getattr(config, "tencent_secret_id", None)
        prev_key = getattr(config, "tencent_secret_key", None)
        try:
            if self.secret_id is not None:
                config.tencent_secret_id = self.secret_id
            if self.secret_key is not None:
                config.tencent_secret_key = self.secret_key
            self._translator = tencent.Translator()
        except TencentCloudSDKException as exc:
            raise ValueError(
                "Missing Tencent API credentials. Set TENCENT_SECRET_ID and TENCENT_SECRET_KEY "
                "environment variables or configure tencent_secret_id / tencent_secret_key."
            ) from exc
        finally:
            config.tencent_secret_id = prev_id
            config.tencent_secret_key = prev_key

        if region != "ap-shanghai" and self.secret_id and self.secret_key:
            self._translator.cred = credential.Credential(self.secret_id, self.secret_key)
            self._translator.client = tmt_client.TmtClient(self._translator.cred, region)

    @property
    def client(self):
        return getattr(self._translator, "client", None)

    @client.setter
    def client(self, value):
        self._translator.client = value

    def is_error_request_frequency(self, exc: BaseException) -> bool:
        if hasattr(self._translator, "is_error_request_frequency") and hasattr(exc, "get_code"):
            return bool(self._translator.is_error_request_frequency(exc))
        return super().is_error_request_frequency(exc)

    @staticmethod
    def _normalize_lang(lang: str) -> str:
        if lang == "zh-CN":
            return "zh"
        return lang

    def translate(self, text: str, language_to: str, language_from: str = "en") -> str:
        if not text:
            return text
        norm_to = self._normalize_lang(language_to)
        norm_from = self._normalize_lang(language_from)
        return self._translator.translate(text, norm_to, norm_from)
