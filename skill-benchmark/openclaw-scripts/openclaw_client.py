"""
OpenClaw client for benchmark automation.
Uses `openclaw agent` CLI which handles device identity and gateway auth.
"""

import json
import subprocess
import shutil


def _find_openclaw() -> str:
    """Find the openclaw binary."""
    path = shutil.which("openclaw")
    if path:
        return path
    # Common locations
    for p in ["/usr/local/bin/openclaw", "/opt/homebrew/bin/openclaw"]:
        import os
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("openclaw CLI not found. Install or add to PATH.")


class OpenClawClient:
    """Client that invokes `openclaw agent` CLI for each message."""

    def __init__(self, timeout: int = 600, agent: str = "main"):
        self.openclaw_bin = _find_openclaw()
        self.timeout = timeout
        self.agent = agent

    def chat(self, session_key: str, message: str) -> str:
        """Send a message and return the full response text."""
        cmd = [
            self.openclaw_bin, "agent",
            "--agent", self.agent,
            "-m", message,
            "--session-id", session_key,
            "--json",
            "--timeout", str(self.timeout),
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout + 30,
        )

        # Parse JSON output (skip plugin log lines)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # Find the JSON object in output
        json_start = stdout.find("{")
        if json_start < 0:
            if proc.returncode != 0:
                raise RuntimeError(
                    f"openclaw agent failed (exit {proc.returncode}): "
                    f"{stderr[:500]}"
                )
            raise RuntimeError(f"No JSON in openclaw output: {stdout[:500]}")

        try:
            result = json.loads(stdout[json_start:])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse openclaw JSON: {e}\n{stdout[:500]}")

        # Extract text from payloads
        payloads = result.get("payloads", [])
        texts = [p.get("text", "") for p in payloads if p.get("text")]
        response = "\n".join(texts)

        # Extract metadata
        meta = result.get("meta", {})
        agent_meta = meta.get("agentMeta", {})
        self._last_meta = {
            "session_id": agent_meta.get("sessionId"),
            "model": agent_meta.get("model"),
            "usage": agent_meta.get("usage", {}),
            "duration_ms": meta.get("durationMs"),
        }

        return response

    @property
    def last_meta(self) -> dict:
        """Metadata from the last chat() call."""
        return getattr(self, "_last_meta", {})
