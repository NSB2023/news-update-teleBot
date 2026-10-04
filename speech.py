"""Speech normalization, safe Pocket TTS chunking, and Telegram Opus encoding."""

import re
import shutil
import subprocess
import wave
from datetime import datetime
from pathlib import Path

import numpy as np


ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = "zero zero twenty thirty forty fifty sixty seventy eighty ninety".split()
ACRONYMS = {
    "AI": "A I", "CEO": "C E O", "GDP": "G D P", "IMF": "I M F",
    "NATO": "NATO", "UK": "United Kingdom", "UN": "United Nations",
    "US": "United States", "USA": "United States", "EU": "European Union",
}
DATE_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh",
    12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
    16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
    20: "twentieth", 21: "twenty first", 22: "twenty second", 23: "twenty third",
    24: "twenty fourth", 25: "twenty fifth", 26: "twenty sixth", 27: "twenty seventh",
    28: "twenty eighth", 29: "twenty ninth", 30: "thirtieth", 31: "thirty first",
}


def integer_words(number):
    number = int(number)
    if number < 20:
        return ONES[number]
    if number < 100:
        return TENS[number // 10] + (" " + ONES[number % 10] if number % 10 else "")
    if number < 1000:
        return ONES[number // 100] + " hundred" + (" " + integer_words(number % 100) if number % 100 else "")
    for value, name in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
        if number >= value:
            return integer_words(number // value) + " " + name + (" " + integer_words(number % value) if number % value else "")
    return str(number)


def number_words(value):
    value = value.replace(",", "")
    if "." in value:
        whole, decimal = value.split(".", 1)
        return integer_words(int(whole)) + " point " + " ".join(ONES[int(d)] for d in decimal)
    return integer_words(int(value))


def normalize_for_speech(text):
    def iso_date(match):
        try:
            value = datetime.strptime(match.group(0), "%Y-%m-%d")
            return f"{value.strftime('%B')} {DATE_ORDINALS[value.day]}, {integer_words(value.year)}"
        except ValueError:
            return match.group(0)

    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", iso_date, text)
    currencies = {"$": " dollars", "£": " pounds", "€": " euros", "৳": " taka"}
    for symbol, unit in currencies.items():
        text = re.sub(re.escape(symbol) + r"\s*([0-9][0-9,]*(?:\.\d+)?)", lambda m: number_words(m.group(1)) + unit, text)
    text = re.sub(r"([0-9][0-9,]*(?:\.\d+)?)\s*%", lambda m: number_words(m.group(1)) + " percent", text)
    for acronym, spoken in ACRONYMS.items():
        text = re.sub(rf"\b{re.escape(acronym)}\b", spoken, text)
    text = re.sub(r"\b[0-9][0-9,]*(?:\.\d+)?\b", lambda m: number_words(m.group(0)), text)
    return re.sub(r"\s+", " ", text).strip()


def speech_chunks(text, max_words=16):
    """Split on sentences, then clauses, then words so Pocket TTS stays below its limit."""
    normalized = normalize_for_speech(text)
    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    chunks = []
    for sentence in sentences:
        if not sentence.strip():
            continue
        clauses = re.split(r"(?<=[,;:])\s+", sentence.strip())
        current = []
        for clause in clauses:
            words = clause.split()
            while len(words) > max_words:
                if current:
                    chunks.append(" ".join(current))
                    current = []
                chunks.append(" ".join(words[:max_words]))
                words = words[max_words:]
            if current and len(current) + len(words) > max_words:
                chunks.append(" ".join(current))
                current = []
            current.extend(words)
        if current:
            chunks.append(" ".join(current))
    return chunks


def save_chunked_wav(model, voice_state, text, path, *, max_words=16, pause_ms=350):
    chunks = speech_chunks(text, max_words=max_words)
    if not chunks:
        raise ValueError("No speakable text was provided")
    pause = np.zeros(int(model.sample_rate * pause_ms / 1000), dtype=np.float32)
    pieces = []
    for index, chunk in enumerate(chunks):
        audio = model.generate_audio(voice_state, chunk).detach().cpu().numpy().astype(np.float32)
        pieces.append(audio)
        if index < len(chunks) - 1:
            pieces.append(pause)
    samples = np.concatenate(pieces)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(model.sample_rate)
        wav.writeframes(pcm.tobytes())
    return len(samples) / model.sample_rate, len(chunks)


def ffmpeg_binary(configured=""):
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("ffmpeg")


def encode_telegram_voice(wav_path, ogg_path, configured_ffmpeg=""):
    binary = ffmpeg_binary(configured_ffmpeg)
    if not binary:
        raise RuntimeError("ffmpeg is required for OGG/Opus. Install it with: brew install ffmpeg")
    subprocess.run(
        [binary, "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", str(ogg_path)],
        check=True,
    )
