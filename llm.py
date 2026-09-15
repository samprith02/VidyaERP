"""
VidyaERP :: LLM transport
Provider-agnostic client for any OpenAI-compatible /chat/completions endpoint
(OpenAI, Groq, OpenRouter, Together, DeepSeek, Fireworks, vLLM, LM Studio, Ollama…).

Zero third-party dependencies — plain urllib, so nothing to install.
Config comes from .env (see .env.example).
"""
import os, json, time, urllib.request, urllib.error

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=ENV_PATH):
    """Minimal .env loader — no python-dotenv dependency."""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            out[k.strip()] = v
            if v:
                os.environ.setdefault(k.strip(), v)
    return out


class LLMConfig:
    def __init__(self):
        self.reload()

    def reload(self):
        env = load_env()
        g = lambda k, d="": (env.get(k) or os.environ.get(k) or d).strip()
        self.provider = g("LLM_PROVIDER", "openai-compatible")
        self.api_key = g("LLM_API_KEY")
        self.base_url = g("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = g("LLM_MODEL", "gpt-4o-mini")
        self.temperature = float(g("LLM_TEMPERATURE", "0.35") or 0.35)
        self.max_tokens = int(g("LLM_MAX_TOKENS", "1400") or 1400)
        self.timeout = int(g("LLM_TIMEOUT", "60") or 60)
        self.fallbacks = [m.strip() for m in g("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
        return self

    @property
    def enabled(self):
        # a local endpoint (ollama / lm-studio / vllm) needs no key
        local = any(h in self.base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker"))
        return bool(self.api_key) or local

    def describe(self):
        return {"enabled": self.enabled, "provider": self.provider, "model": self.model,
                "base_url": self.base_url, "key_set": bool(self.api_key),
                "fallbacks": self.fallbacks, "limits": LAST_LIMITS,
                "mode": "llm" if self.enabled else "rule-engine"}


LAST_LIMITS = {}


CFG = LLMConfig()


class LLMError(Exception):
    def __init__(self, msg, status=None, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def chat(messages, tools=None, cfg=None, tool_choice="auto", model=None):
    """One round-trip. Returns (assistant_message_dict, usage_dict, latency_ms)."""
    cfg = cfg or CFG
    if not cfg.enabled:
        raise LLMError("no API key configured")
    body = {"model": model or cfg.model, "messages": messages,
            "temperature": cfg.temperature, "max_tokens": cfg.max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    req = urllib.request.Request(
        cfg.base_url + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.api_key or 'local'}",
                 # Groq/Cloudflare rejects the default "Python-urllib" agent with error 1010
                 "User-Agent": "VidyaERP/1.0 (+https://vidyatech.edu.in)",
                 "Accept": "application/json",
                 "HTTP-Referer": "https://vidyaerp.local",     # OpenRouter etiquette
                 "X-Title": "VidyaERP"},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            data = json.loads(r.read().decode())
            _capture_limits(r.headers, model or cfg.model)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        _capture_limits(e.headers, model or cfg.model)
        ra = e.headers.get("retry-after") if e.headers else None
        raise LLMError(f"HTTP {e.code}: {detail}", status=e.code,
                       retry_after=float(ra) if ra else None)
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}")
    ms = int((time.time() - t0) * 1000)
    if "choices" not in data:
        raise LLMError(f"unexpected response: {json.dumps(data)[:300]}")
    msg = data["choices"][0]["message"]
    usage = data.get("usage", {}) or {}
    return msg, usage, ms


def _capture_limits(headers, model):
    if not headers:
        return
    try:
        LAST_LIMITS[model] = {
            "tokens_remaining": headers.get("x-ratelimit-remaining-tokens"),
            "tokens_limit": headers.get("x-ratelimit-limit-tokens"),
            "reset": headers.get("x-ratelimit-reset-tokens")}
    except Exception:
        pass


def chat_resilient(messages, tools=None, cfg=None):
    """Try the primary model, then each fallback, on rate-limit / server errors.

    Groq's quota is per-model, so a fallback chain multiplies usable throughput
    instead of dropping the user straight onto the rule engine.
    """
    cfg = cfg or CFG
    chain = [cfg.model] + [m for m in cfg.fallbacks if m != cfg.model]
    last = None
    for i, model in enumerate(chain):
        try:
            msg, usage, ms = chat(messages, tools, cfg, model=model)
            return msg, usage, ms, model, i
        except LLMError as e:
            last = e
            retryable = e.status in (429, 500, 502, 503) or (
                e.status == 400 and "tool_use_failed" in str(e))
            if retryable and i < len(chain) - 1:
                continue
            raise
    raise last


def ping(cfg=None):
    """Health-check used by /api/mode and the UI badge."""
    cfg = cfg or CFG
    if not cfg.enabled:
        return {"ok": False, "error": "no API key configured"}
    try:
        msg, usage, ms = chat([{"role": "user", "content": "Reply with the single word: ready"}], cfg=cfg)
        return {"ok": True, "latency_ms": ms, "model": cfg.model,
                "reply": (msg.get("content") or "").strip()[:40]}
    except LLMError as e:
        return {"ok": False, "error": str(e)}
