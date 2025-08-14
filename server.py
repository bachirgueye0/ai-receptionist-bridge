import os, json, asyncio
from aiohttp import web
import websockets

# --- ENV VARS (set these on Render) ---
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_REALTIME_URL = os.environ.get(
    "OPENAI_REALTIME_URL",
    "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
)
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Your Company")

SYSTEM_PROMPT = f"""
You are the phone receptionist for {COMPANY_NAME}.
- Greet warmly; verify caller name + callback number early.
- Classify: emergency | schedule | quote | general.
- For emergencies: collect ADDRESS first, then issue + preferred time.
- Keep each reply under 7 seconds. Be concise; allow interruptions.
- Confirm details back before any booking. Never claim to be AI.
"""

async def openai_session():
    return await websockets.connect(
        OPENAI_REALTIME_URL,
        extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        max_size=None
    )

async def twilio_stream_handler(request):
    # Twilio connects here as a WebSocket
    ws = web.WebSocketResponse(protocols=("audio.ws.twilio.com",))
    await ws.prepare(request)

    # Connect to OpenAI Realtime
    oai = await openai_session()

    # Configure Realtime (8kHz µ-law both ways for Twilio)
    await oai.send(json.dumps({
        "type":"session.update",
        "session":{
            "instructions": SYSTEM_PROMPT,
            "input_audio_format":{"type":"g711_ulaw","sample_rate_hz":8000},
            "output_audio_format":{"type":"g711_ulaw","sample_rate_hz":8000}
        }
    }))

    async def oai_to_twilio():
        async for msg in oai:
            try:
                data = json.loads(msg)
            except Exception:
                continue
            t = data.get("type")
            if t == "output_audio.delta":
                # stream assistant audio back to the caller
                await ws.send_json({"event":"media","media":{"payload": data["audio"]}})
            elif t == "response.completed":
                await ws.send_json({"event":"mark","mark":{"name":"response_end"}})

    async def twilio_to_oai():
        # kick off first response after our Step-1 greeting
        await oai.send(json.dumps({"type":"response.create","response":{}}))
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            payload = json.loads(msg.data)
            ev = payload.get("event")
            if ev == "media":
                # caller audio → OpenAI
                await oai.send(json.dumps({
                    "type":"input_audio_buffer.append",
                    "audio": payload["media"]["payload"]
                }))
            elif ev == "stop":
                # end of caller turn → commit and ask assistant to reply
                await oai.send(json.dumps({"type":"input_audio_buffer.commit"}))
                await oai.send(json.dumps({"type":"response.create","response":{}}))

    try:
        await asyncio.gather(oai_to_twilio(), twilio_to_oai())
    finally:
        await ws.close()
        await oai.close()
    return ws

async def health(request):
    return web.Response(text="OK: bridge is running")

app = web.Application()
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.router.add_get("/twilio-stream", twilio_stream_handler)

if __name__ == "__main__":
    web.run_app(app, port=int(os.environ.get("PORT", 8080)))
