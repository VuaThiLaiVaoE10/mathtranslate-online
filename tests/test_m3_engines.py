from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from mathtranslate.engines import (
    AVAILABLE_ENGINES,
    BaseTranslator,
    DeepLTranslator,
    GeminiTranslator,
    GoogleTranslator,
    OllamaTranslator,
    OpenAICompatTranslator,
    OpenAITranslator,
    TencentTranslator,
    create_engine,
    normalize_deepl_language,
)
from mathtranslate.tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)


def _mock_response(json_data, status_code=200):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        err = requests.HTTPError(f"HTTP {status_code}", response=resp)
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_available_engines_and_registry(monkeypatch):
    assert AVAILABLE_ENGINES == ("google", "tencent", "gemini", "openai", "ollama", "deepl")

    monkeypatch.setenv("TENCENT_SECRET_ID", "test-tencent-id")
    monkeypatch.setenv("TENCENT_SECRET_KEY", "test-tencent-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("DEEPL_API_KEY", "test-deepl-key:fx")

    expected_classes = {
        "google": GoogleTranslator,
        "tencent": TencentTranslator,
        "gemini": GeminiTranslator,
        "openai": OpenAITranslator,
        "ollama": OllamaTranslator,
        "deepl": DeepLTranslator,
    }
    for name, expected_cls in expected_classes.items():
        engine = create_engine(name)
        assert isinstance(engine, BaseTranslator)
        assert isinstance(engine, expected_cls)
        assert engine.name == name

    # Both openai and ollama are OpenAICompatTranslator instances
    assert isinstance(create_engine("openai"), OpenAICompatTranslator)
    assert isinstance(create_engine("ollama"), OpenAICompatTranslator)

    with pytest.raises(ValueError, match="Unknown translation engine"):
        create_engine("unknown_engine")

    with pytest.raises(ValueError, match="Invalid engine name"):
        create_engine("")


@pytest.mark.parametrize(
    "engine_name,env_var",
    [
        ("gemini", "GEMINI_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("deepl", "DEEPL_API_KEY"),
    ],
)
def test_missing_api_keys_raise_clear_value_error(monkeypatch, engine_name, env_var):
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(ValueError) as exc_info:
        create_engine(engine_name)
    msg = str(exc_info.value)
    assert env_var in msg
    assert "Missing" in msg


def test_google_engine_googleapis_primary_and_xmathx_preservation():
    engine = create_engine("google")
    assert engine.supports_multithread is True

    sample_input = "Consider the formula XMATHX_0 in quantum mechanics."
    sample_output = "Xét công thức XMATHX_0 trong cơ học lượng tử."
    googleapis_payload = [
        [[sample_output, sample_input, None, None, 10]],
        None,
        "en",
    ]
    with mock.patch("mathtranslate.engines.google_engine.requests.get") as mock_get, mock.patch(
        "mathtranslate.engines.google_engine.mtranslate.translate"
    ) as mock_mtranslate:
        mock_get.return_value = _mock_response(googleapis_payload, 200)
        translated = engine.translate(sample_input, language_to="vi", language_from="en")

        assert translated == sample_output
        assert "XMATHX_0" in translated
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["client"] == "gtx"
        assert kwargs["params"]["sl"] == "en"
        assert kwargs["params"]["tl"] == "vi"
        assert kwargs["params"]["dt"] == "t"
        assert kwargs["params"]["q"] == sample_input
        assert "XMATHX_0" in kwargs["params"]["q"]
        assert kwargs["timeout"] == 15
        assert "Mozilla/5.0" in kwargs["headers"]["User-Agent"]
        mock_mtranslate.assert_not_called()


def test_google_engine_falls_back_to_mtranslate_on_error():
    engine = create_engine("google")
    sample_input = "Theorem XMATHX_0 holds for all primes."
    fallback_output = "Định lý XMATHX_0 đúng với mọi số nguyên tố."

    with mock.patch("mathtranslate.engines.google_engine.requests.get") as mock_get, mock.patch(
        "mathtranslate.engines.google_engine.mtranslate.translate",
        return_value=fallback_output,
    ) as mock_mtranslate:
        mock_get.return_value = _mock_response({"error": "rate limit"}, status_code=429)
        translated = engine.translate(sample_input, language_to="vi", language_from="en")

        assert translated == fallback_output
        assert "XMATHX_0" in translated
        mock_get.assert_called_once()
        mock_mtranslate.assert_called_once_with(sample_input, "vi", "en")


def test_tencent_engine_wraps_legacy_translator_and_preserves_xmathx():
    engine = create_engine("tencent", secret_id="id-123", secret_key="key-456")
    assert engine.supports_multithread is False

    sample_input = "We substitute XMATHX_0 into equation XMATHX_1."
    expected_output = "Chúng ta thay XMATHX_0 vào phương trình XMATHX_1."

    mock_client = mock.MagicMock()
    mock_client.TextTranslate.return_value = SimpleNamespace(TargetText=expected_output)
    engine.client = mock_client

    translated = engine.translate(sample_input, language_to="zh-CN", language_from="en")
    assert translated == expected_output
    assert "XMATHX_0" in translated

    mock_client.TextTranslate.assert_called_once()
    req_arg = mock_client.TextTranslate.call_args[0][0]
    assert req_arg.SourceText == sample_input
    assert "XMATHX_0" in req_arg.SourceText
    assert req_arg.Source == "en"
    assert req_arg.Target == "zh"  # zh-CN normalized to zh for Tencent TMT
    assert req_arg.UntranslatedText == "XMATHX"

    rate_exc = TencentCloudSDKException("RequestLimitExceeded", "rate limit")
    other_exc = TencentCloudSDKException("AuthFailure", "bad key")
    assert engine.is_error_request_frequency(rate_exc) is True
    assert engine.is_error_request_frequency(other_exc) is False
    assert engine.is_retryable_error(rate_exc) is True


def test_openai_engine_sends_and_preserves_xmathx(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-123")
    engine = create_engine("openai")
    assert engine.model == "gpt-4o-mini"
    assert engine.endpoint == "https://api.openai.com/v1/chat/completions"

    sample_input = "Let XMATHX_0 be a compact Riemannian manifold."
    expected_output = "Giả sử XMATHX_0 là một đa tạp Riemann compact."

    with mock.patch("mathtranslate.engines.openai_compat.requests.post") as mock_post:
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"  {expected_output}  ",
                        }
                    }
                ]
            },
            200,
        )
        translated = engine.translate(sample_input, language_to="vi", language_from="en")

        assert translated == expected_output
        assert "XMATHX_0" in translated
        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert url == "https://api.openai.com/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test-openai-123"
        payload = kwargs["json"]
        assert payload["model"] == "gpt-4o-mini"
        assert "XMATHX" in payload["messages"][0]["content"]
        assert payload["messages"][1]["content"] == sample_input
        assert "XMATHX_0" in payload["messages"][1]["content"]


def test_ollama_engine_works_without_api_key_and_preserves_xmathx(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")

    engine = create_engine("ollama")
    assert engine.model == "qwen2.5:7b"
    assert engine.endpoint == "http://localhost:11434/v1/chat/completions"
    assert engine.api_key is None

    sample_input = "The eigenvalue XMATHX_0 satisfies the boundary condition."
    expected_output = "Giá trị riêng XMATHX_0 thỏa mãn điều kiện biên."

    with mock.patch("mathtranslate.engines.openai_compat.requests.post") as mock_post:
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": expected_output,
                        }
                    }
                ]
            },
            200,
        )
        translated = engine.translate(sample_input, language_to="vi", language_from="en")

        assert translated == expected_output
        assert "XMATHX_0" in translated
        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert url == "http://localhost:11434/v1/chat/completions"
        assert "Authorization" not in kwargs["headers"]
        payload = kwargs["json"]
        assert payload["model"] == "qwen2.5:7b"
        assert "XMATHX" in payload["messages"][0]["content"]
        assert payload["messages"][1]["content"] == sample_input
        assert "XMATHX_0" in payload["messages"][1]["content"]


def test_gemini_engine_sends_and_preserves_xmathx(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-gemini-key")
    engine = create_engine("gemini")
    assert engine.model == "gemini-2.0-flash"
    assert (
        engine.endpoint
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    )

    sample_input = "Integrating XMATHX_0 over the domain yields zero."
    expected_output = "Tích phân XMATHX_0 trên miền đã cho bằng không."

    with mock.patch("mathtranslate.engines.gemini_engine.requests.post") as mock_post:
        mock_post.return_value = _mock_response(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": expected_output}],
                        }
                    }
                ]
            },
            200,
        )
        translated = engine.translate(sample_input, language_to="vi", language_from="en")

        assert translated == expected_output
        assert "XMATHX_0" in translated
        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert (
            url
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        )
        assert kwargs["headers"]["x-goog-api-key"] == "AIza-test-gemini-key"
        assert "params" not in kwargs
        payload = kwargs["json"]
        sys_text = payload["systemInstruction"]["parts"][0]["text"]
        user_text = payload["contents"][0]["parts"][0]["text"]
        assert "XMATHX" in sys_text
        assert user_text == sample_input
        assert "XMATHX_0" in user_text


def test_deepl_engine_free_and_pro_endpoints_and_xmathx_preservation():
    free_engine = create_engine("deepl", api_key="deepl-secret-123:fx")
    pro_engine = create_engine("deepl", api_key="deepl-pro-secret-456")

    assert free_engine.endpoint == "https://api-free.deepl.com/v2/translate"
    assert pro_engine.endpoint == "https://api.deepl.com/v2/translate"

    assert normalize_deepl_language("en") == "EN"
    assert normalize_deepl_language("vi") == "VI"
    assert normalize_deepl_language("zh-CN") == "ZH-HANS"
    assert normalize_deepl_language("zh-TW") == "ZH-HANT"

    sample_input = "Hence XMATHX_0 converges uniformly."
    expected_output = "Do đó XMATHX_0 hội tụ đều."

    with mock.patch("mathtranslate.engines.deepl_engine.requests.post") as mock_post:
        mock_post.return_value = _mock_response(
            {"translations": [{"detected_source_language": "EN", "text": expected_output}]},
            200,
        )
        translated = free_engine.translate(sample_input, language_to="zh-CN", language_from="en")

        assert translated == expected_output
        assert "XMATHX_0" in translated
        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert url == "https://api-free.deepl.com/v2/translate"
        assert kwargs["headers"]["Authorization"] == "DeepL-Auth-Key deepl-secret-123:fx"
        payload = kwargs["json"]
        assert payload["text"] == [sample_input]
        assert "XMATHX_0" in payload["text"][0]
        assert payload["source_lang"] == "EN"
        assert payload["target_lang"] == "ZH-HANS"


def test_is_retryable_error_helper():
    engine = create_engine("google")
    assert engine.is_retryable_error(TimeoutError("timed out")) is True
    assert engine.is_retryable_error(requests.ConnectionError("conn reset")) is True

    resp_429 = _mock_response({}, status_code=429)
    err_429 = requests.HTTPError("429 Too Many Requests", response=resp_429)
    assert engine.is_retryable_error(err_429) is True

    resp_401 = _mock_response({}, status_code=401)
    err_401 = requests.HTTPError("401 Unauthorized", response=resp_401)
    assert engine.is_retryable_error(err_401) is False
    assert engine.is_retryable_error(ValueError("bad config")) is False
