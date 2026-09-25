"""Generate a WAV file using the installed Pocket TTS model."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Text to speak")
    parser.add_argument("--voice", default="alba", help="Built-in voice or reference audio path")
    parser.add_argument("--output", type=Path, default=Path("output.wav"))
    args = parser.parse_args()
    if not args.text.strip():
        parser.error("text must not be empty")

    import scipy.io.wavfile
    from pocket_tts import TTSModel

    model = TTSModel.load_model()
    voice_state = model.get_state_for_audio_prompt(args.voice)
    audio = model.generate_audio(voice_state, args.text)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    scipy.io.wavfile.write(
        str(args.output), model.sample_rate, audio.detach().cpu().numpy()
    )
    print(f"Saved speech to {args.output.resolve()}")


if __name__ == "__main__":
    main()
