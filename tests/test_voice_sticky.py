import asyncio
from agent import voice
async def edge_ok(text, voice_name, path):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3", str(path)], check=True)
def test_engine_that_spoke_stays_first(tmp_path):
    v = voice.Voice(None, tmp_path, edge=edge_ok)
    assert v.orpheus_first is True
    asyncio.run(v.synthesize("Hello."))
    assert v.last["tts"].startswith("edge-tts") and v.orpheus_first is False
    asyncio.run(v.synthesize("Hello again."))
    assert v.last["tts"].startswith("edge-tts") and "errors" not in v.last
