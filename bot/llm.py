import base64
import json
import urllib.request
import ssl


IMAGE_MAX_BYTES = 15 * 1024 * 1024


def _fetch_image_b64(url, timeout=15, max_bytes=IMAGE_MAX_BYTES):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "") or ""
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None, None
        if not ctype.startswith("image"):
            return None, None
        return base64.b64encode(data).decode("ascii"), ctype
    except Exception:
        return None, None


def generate_reply(config, system_text, user_parts):
    content = []
    for part in user_parts:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = part["image_url"]["url"]
            b64, ctype = _fetch_image_b64(url)
            if b64:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": "data:%s;base64,%s" % (ctype, b64)},
                })
            else:
                content.append(part)
        else:
            content.append(part)

    if len(content) == 1 and content[0].get("type") == "text":
        user_content = content[0]["text"]
    else:
        user_content = content

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_content},
    ]
    return call_llm(messages, config)


def call_llm(messages, config):
    cfg = config.get("llm", {})
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "gpt-3.5-turbo")
    url = base_url + "/chat/completions"

    body = {
        "model": model,
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.8)),
        "max_tokens": int(cfg.get("max_tokens", 800)),
        "stream": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    }

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    ctx = ssl.create_default_context()
    timeout = int(cfg.get("timeout", 60))

    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8")

    result = json.loads(raw)
    return result["choices"][0]["message"]["content"].strip()
