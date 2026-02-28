#!/usr/bin/env python3
"""
voice_message.py
Send a Telegram voice message using ElevenLabs TTS.

Reads message config from voice_message.json (same directory).

Usage:
    python scripts/voice_message.py                    # send from voice_message.json
    python scripts/voice_message.py message.json       # send from custom JSON file

JSON format:
    {
        "text": "Hey, here is your update.",
        "language": "en",
        "stability": 0.5,
        "speed": 1.2,
        "caption": "Optional Telegram caption"
    }

Only "text" is required. All other fields have defaults.

Voice routing:
  German (language="de") uses a native German voice. All other languages use the default voice.

Model selection (automatic):
  eleven_v3 for German and English. Audio Tags ([excited], [pause], etc.) work natively.
  For other languages, falls back to eleven_multilingual_v2 when a pronunciation dictionary
  is configured and the trigger word appears in the text. Audio Tags are stripped in v2
  (correct pronunciation is prioritized over emotion control).

Pronunciation dictionary (optional, v2 only, not used for de/en):
  ElevenLabs v2 sometimes mispronounces names, especially when mixing languages.
  A pronunciation dictionary lets you define how specific words should sound.
  Not needed for German (native voice handles it) or English (v3 handles it).
  Create one from a PLS file (see scripts/pronunciation.example.pls):

    1. Edit scripts/pronunciation.example.pls with your word and alias
    2. Run: python scripts/upload_pronunciation.py scripts/pronunciation.example.pls
    3. Copy the printed IDs into .env
    4. Set ELEVENLABS_PRONUNCIATION_WORD to the trigger word (e.g. "Michael")

Environment variables (from .env):
    ELEVENLABS_API_KEY                          - required
    TELEGRAM_BOT_TOKEN                          - required
    TELEGRAM_CHAT_ID                            - required
    ELEVENLABS_PRONUNCIATION_DICT_ID            - optional
    ELEVENLABS_PRONUNCIATION_DICT_VERSION       - optional
    ELEVENLABS_PRONUNCIATION_WORD               - optional (default: disabled)
"""

import sys
import io
import json
import functools
from pathlib import Path

if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer') and getattr(sys.stdout, 'encoding', '').lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

print = functools.partial(print, flush=True)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
    pass

import json as _json_mod
import os
import re
import struct
import subprocess
from datetime import datetime
import requests

ELEVENLABS_KEY = os.environ.get('ELEVENLABS_API_KEY', '')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

VOICE_EN = 'MFZUKuGQUsGJPQjTS4wC'
VOICE_DE = 'g1jpii0iyvtRs8fqXsd1'
DEFAULT_SPEED = 1.2
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY = 0.5

MODEL_V2 = 'eleven_multilingual_v2'
MODEL_V3 = 'eleven_v3'

PRONUNCIATION_DICT_ID = os.environ.get('ELEVENLABS_PRONUNCIATION_DICT_ID', '')
PRONUNCIATION_DICT_VERSION = os.environ.get('ELEVENLABS_PRONUNCIATION_DICT_VERSION', '')
PRONUNCIATION_WORD = os.environ.get('ELEVENLABS_PRONUNCIATION_WORD', '')

AUDIO_TAG_RE = re.compile(r'\[(?:excited|sad|whisper|angry|laughs|pause|slows down|rushed)\]', re.IGNORECASE)

_SAMPLE_RATE = 48000
_WINDOW = _SAMPLE_RATE * 30 // 1000  # 30ms frames
_QUALITY_LOG = Path(__file__).resolve().parent.parent / 'logs' / 'voice_quality.jsonl'
_QUALITY_LOG_MAX = 50


def _audio_quality_log(opus_bytes, voice_id, model, language):
    """Decode opus to PCM, measure CrestMax and Peak of first 2 seconds.
    Logs to stdout and appends to logs/voice_quality.jsonl (capped at 50 entries)."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', 'pipe:0', '-f', 's16le', '-ac', '1', '-ar', str(_SAMPLE_RATE), 'pipe:1'],
            input=opus_bytes, capture_output=True, timeout=15)
        if result.returncode != 0:
            return
        raw = result.stdout
        n_samples = len(raw) // 2
        if n_samples < _SAMPLE_RATE:
            return
        samples = struct.unpack(f'<{n_samples}h', raw)
        head_end = min(_SAMPLE_RATE * 2, n_samples)
        head = samples[:head_end]
        n_frames = len(head) // _WINDOW
        if n_frames < 2:
            return
        peak = max(abs(s) for s in head) / 32768.0
        max_crest = 0.0
        for i in range(n_frames):
            frame = head[i * _WINDOW:(i + 1) * _WINDOW]
            rms_sq = sum(s * s for s in frame) / len(frame)
            if rms_sq < 1.0:
                continue
            pk = max(abs(s) for s in frame)
            cf = pk / rms_sq ** 0.5
            if cf > max_crest:
                max_crest = cf
        print(f'[elevenlabs] Audio quality: Peak={peak:.3f} CrestMax={max_crest:.1f}')

        _QUALITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = _json_mod.dumps({
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'voice': voice_id, 'model': model, 'lang': language or 'auto',
            'peak': round(peak, 3), 'crest_max': round(max_crest, 1),
            'bytes': len(opus_bytes),
        })
        lines = []
        if _QUALITY_LOG.exists():
            lines = _QUALITY_LOG.read_text(encoding='utf-8').strip().splitlines()
        lines.append(entry)
        lines = lines[-_QUALITY_LOG_MAX:]
        _QUALITY_LOG.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[elevenlabs] Audio quality check failed: {e}')


def _needs_pronunciation_dict(text):
    if not (PRONUNCIATION_DICT_ID and PRONUNCIATION_DICT_VERSION and PRONUNCIATION_WORD):
        return False
    return bool(re.search(r'\b' + re.escape(PRONUNCIATION_WORD) + r'\b', text, re.IGNORECASE))


def send_voice_message(config):
    text = config['text']
    language = config.get('language')
    stability = config.get('stability', DEFAULT_STABILITY)
    speed = config.get('speed', DEFAULT_SPEED)
    similarity = config.get('similarity_boost', DEFAULT_SIMILARITY)
    caption = config.get('caption')

    has_audio_tags = bool(AUDIO_TAG_RE.search(text))

    # 1. Voice: German uses a native German voice, everything else uses default
    if language == 'de':
        voice_id = config.get('voice_id', VOICE_DE)
    else:
        voice_id = config.get('voice_id', VOICE_EN)

    # 2. Model: V3 by default (Audio Tags work natively).
    #    V2 only as fallback when pronunciation dict is needed (dict requires V2).
    use_dict = _needs_pronunciation_dict(text) and language not in ('de', 'en')
    if use_dict:
        model = MODEL_V2
        if has_audio_tags:
            text = AUDIO_TAG_RE.sub('', text).strip()
    else:
        model = MODEL_V3

    url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=opus_48000_64'
    payload = {
        'text': text,
        'model_id': model,
        'voice_settings': {
            'stability': stability,
            'similarity_boost': similarity,
            'speed': speed,
        },
    }
    if language:
        payload['language_code'] = language
    if use_dict:
        payload['pronunciation_dictionary_locators'] = [
            {'pronunciation_dictionary_id': PRONUNCIATION_DICT_ID, 'version_id': PRONUNCIATION_DICT_VERSION}
        ]

    print(f'[elevenlabs] Generating speech ({len(text)} chars, model={model}, voice={voice_id}, lang={language or "auto"}, stability={stability}, speed={speed})')
    if use_dict:
        print(f'[elevenlabs] Using pronunciation dictionary ({PRONUNCIATION_WORD})')
    resp = requests.post(url, headers={'xi-api-key': ELEVENLABS_KEY, 'Content-Type': 'application/json'}, json=payload)

    if resp.status_code != 200:
        print(f'[elevenlabs] Error {resp.status_code}: {resp.text[:300]}')
        sys.exit(1)

    audio = resp.content
    print(f'[elevenlabs] Generated {len(audio)} bytes')
    _audio_quality_log(audio, voice_id, model, language)

    tg_data = {'chat_id': CHAT_ID}
    if caption:
        tg_data['caption'] = caption

    r = requests.post(
        f'https://api.telegram.org/bot{TG_TOKEN}/sendVoice',
        data=tg_data,
        files={'voice': ('voice.ogg', io.BytesIO(audio), 'audio/ogg')},
    )
    if r.ok:
        print('[telegram] Voice message sent')
    else:
        print(f'[telegram] Error: {r.text[:300]}')
        sys.exit(1)


def main():
    if len(sys.argv) > 1:
        json_path = Path(sys.argv[1])
    else:
        json_path = Path(__file__).parent / 'voice_message.json'

    if not json_path.exists():
        print(f'File not found: {json_path}')
        sys.exit(1)

    config = json.loads(json_path.read_text(encoding='utf-8'))
    send_voice_message(config)


if __name__ == '__main__':
    main()
