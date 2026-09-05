"""
providers.py - detects which AI provider an API key belongs to (by its
shape/prefix) and exposes one unified async function, generate_text(),
that calls the right provider automatically. The owner never has to say
which provider a key is for.
"""

import json
import re
import httpx

TIMEOUT = httpx.Timeout(60.0)


def detect_provider(raw_key: str):
    """
    Returns (provider_name, cleaned_key, extra) or (None, None, None) if unrecognized.
    extra is used for providers that need more than just a key (e.g. Cloudflare
    needs an account id, passed as 'cf:<account_id>:<token>').
    """
    key = raw_key.strip()

    # Cloudflare Workers AI: owner must paste as cf:<account_id>:<api_token>
    if key.lower().startswith("cf:"):
        parts = key.split(":")
        if len(parts) == 3:
            return "cloudflare", parts[2], parts[1]
        return None, None, None

    if key.startswith("AIza"):
        return "gemini", key, None

    if key.startswith("sk-or-"):
        return "openrouter", key, None

    if key.startswith("gsk_"):
        return "groq", key, None

    if key.startswith("hf_"):
        return "huggingface", key, None

    if key.startswith("csk-"):
        return "cerebras", key, None

    if key.startswith("github_pat_") or key.startswith("ghp_"):
        return "github", key, None

    # Mistral keys are typically 32 lowercase hex/alnum chars
    if re.fullmatch(r"[a-zA-Z0-9]{32}", key):
        return "mistral", key, None

    # OpenAI-style, but not matched above (sk- without sk-or-)
    if key.startswith("sk-"):
        return "openai", key, None

    return None, None, None


# ---- per-provider request builders. Each returns (url, headers, json_body, parse_fn) ----

def _openai_style(base_url, model, key, prompt, extra_headers=None):
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
    }

    def parse(data):
        return data["choices"][0]["message"]["content"]

    return url, headers, body, parse


def build_request(provider: str, key: str, extra: str | None, prompt: str):
    if provider == "gemini":
        model = "gemini-2.0-flash"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}"
        )
        headers = {"Content-Type": "application/json"}
        body = {"contents": [{"parts": [{"text": prompt}]}]}

        def parse(data):
            return data["candidates"][0]["content"]["parts"][0]["text"]

        return url, headers, body, parse

    if provider == "openrouter":
        return _openai_style(
            "https://openrouter.ai/api/v1",
            "meta-llama/llama-3.3-70b-instruct:free",
            key, prompt,
        )

    if provider == "groq":
        return _openai_style(
            "https://api.groq.com/openai/v1",
            "llama-3.3-70b-versatile",
            key, prompt,
        )

    if provider == "cerebras":
        return _openai_style(
            "https://api.cerebras.ai/v1",
            "llama3.3-70b",
            key, prompt,
        )

    if provider == "github":
        return _openai_style(
            "https://models.inference.ai.azure.com",
            "gpt-4o-mini",
            key, prompt,
        )

    if provider == "mistral":
        return _openai_style(
            "https://api.mistral.ai/v1",
            "open-mistral-7b",
            key, prompt,
        )

    if provider == "openai":
        return _openai_style(
            "https://api.openai.com/v1",
            "gpt-4o-mini",
            key, prompt,
        )

    if provider == "huggingface":
        model = "mistralai/Mistral-7B-Instruct-v0.3"
        url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"inputs": prompt, "parameters": {"max_new_tokens": 512}}

        def parse(data):
            if isinstance(data, list):
                return data[0]["generated_text"]
            return data.get("generated_text", "")

        return url, headers, body, parse

    if provider == "cloudflare":
        account_id = extra
        model = "@cf/meta/llama-3.1-8b-instruct"
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"messages": [{"role": "user", "content": prompt}]}

        def parse(data):
            return data["result"]["response"]

        return url, headers, body, parse

    raise ValueError(f"Unknown provider: {provider}")


async def generate_text(provider: str, key: str, extra: str | None, prompt: str) -> str:
    """Call the given provider/key and return the raw text response."""
    url, headers, body, parse = build_request(provider, key, extra, prompt)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return parse(data)


async def generate_quiz_json(provider: str, key: str, extra: str | None, topic: str, level: str = "Class 6-10") -> dict:
    """
    Asks the AI for one multiple-choice question as strict JSON:
    {"question": str, "options": [str, str, str, str], "correct_index": int}
    `level` scales difficulty, e.g. "Class 1-5", "Class 6-10", "Class 11-12", "Graduation".
    """
    prompt = (
        f"Create ONE multiple-choice quiz question suitable for '{level}' level students, "
        f"on the subject/topic: '{topic}'. Match the difficulty and vocabulary to that level. "
        f"Respond with ONLY raw JSON, no markdown fences, no extra text, "
        f'in this exact shape: {{"question": "...", "options": ["...", "...", "...", "..."], '
        f'"correct_index": 0}}. correct_index is the 0-based index of the correct option '
        f"in the options array. Keep the question under 200 characters and each option "
        f"under 90 characters (Telegram poll limits)."
    )
    raw = await generate_text(provider, key, extra, prompt)
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return json.loads(cleaned)


async def generate_motivation(provider: str, key: str, extra: str | None, correct: bool, mention: str) -> str:
    outcome = "answered correctly" if correct else "answered incorrectly"
    prompt = (
        f"Write ONE short, punchy, encouraging message (max 25 words) in a "
        f"hacker/cybersecurity tone for a user who just {outcome} in a quiz. "
        f"Mention them as {mention} naturally in the sentence. No hashtags."
    )
    return await generate_text(provider, key, extra, prompt)


async def generate_shayari(provider: str, key: str, extra: str | None, correct: bool, mention: str) -> str:
    """
    Short Hinglish, Instagram-caption-style shayari reacting to a quiz answer.
    Correct -> witty/funny victory shayari. Wrong -> funny 'sad-alone' style
    (the self-deprecating humor common in Instagram reels), never actually
    discouraging or mean-spirited.
    """
    if correct:
        mood = (
            "a witty, funny, celebratory 2-line Hinglish shayari for winning/"
            "getting it right, Instagram-caption style, lighthearted"
        )
    else:
        mood = (
            "a funny, dramatic, mock-'sad and alone' 2-line Hinglish shayari "
            "about getting the answer wrong, in the exaggerated comic style of "
            "Instagram reels captions — funny, never genuinely sad or mean"
        )
    prompt = (
        f"Write {mood}. Naturally include {mention} in it. Max 30 words total. "
        f"No hashtags, no emojis explanation, just the shayari text."
    )
    return await generate_text(provider, key, extra, prompt)


async def generate_reply(provider: str, key: str, extra: str | None, user_message: str) -> str:
    prompt = (
        "You are KRX AI Smart, a helpful, friendly assistant inside a Telegram "
        "cybersecurity quiz bot. Reply naturally and concisely to this message:\n\n"
        f"{user_message}"
    )
    return await generate_text(provider, key, extra, prompt)
