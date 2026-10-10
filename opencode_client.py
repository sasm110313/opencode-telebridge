import asyncio
import json
import logging
import os
import shutil
import urllib.parse
import aiohttp
from typing import AsyncGenerator, Callable, Dict, Any, Optional

logger = logging.getLogger("OpenCodeClient")

class OpenCodeClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        auto_start: bool = True,
        command: str = "opencode",
        workspace_dir: str = "",
        password: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.auto_start = auto_start
        self.command = command
        self.workspace_dir = workspace_dir or os.getcwd()
        self.password = password
        self.server_process: Optional[asyncio.subprocess.Process] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._event_task: Optional[asyncio.Task] = None
        self._event_listeners = []
        self._is_closing = False

    async def get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self.password:
                headers["Authorization"] = f"Bearer {self.password}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def check_server_alive(self) -> bool:
        """Check if OpenCode server is responding."""
        try:
            session = await self.get_http_session()
            async with session.get(f"{self.base_url}/session", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                return resp.status in (200, 401, 403)
        except Exception:
            return False

    async def ensure_server_running(self) -> bool:
        """Ensure the server is running, or start it if auto_start is True."""
        if await self.check_server_alive():
            logger.info("OpenCode server is already running and reachable at %s", self.base_url)
            return True

        if not self.auto_start:
            logger.error("OpenCode server is not running and auto_start is disabled.")
            return False

        # Attempt to find executable
        cmd_path = shutil.which(self.command) or self.command
        logger.info("Starting OpenCode server via: %s serve in %s", cmd_path, self.workspace_dir)

        # Parse port and host from base_url
        # e.g. http://127.0.0.1:4096
        parts = self.base_url.replace("http://", "").replace("https://", "").split(":")
        host = parts[0]
        port = parts[1] if len(parts) > 1 else "4096"

        args = ["serve", f"--hostname={host}", f"--port={port}"]
        env = os.environ.copy()
        if self.password:
            env["OPENCODE_SERVER_PASSWORD"] = self.password

        try:
            self.server_process = await asyncio.create_subprocess_exec(
                cmd_path,
                *args,
                cwd=self.workspace_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            # Log output in background
            asyncio.create_task(self._log_server_output(self.server_process))

            # Wait for server to come online
            for _ in range(30):
                await asyncio.sleep(0.5)
                if await self.check_server_alive():
                    logger.info("OpenCode server started successfully on %s", self.base_url)
                    return True
                if self.server_process.returncode is not None:
                    logger.error("OpenCode server process exited prematurely with code %s", self.server_process.returncode)
                    break
        except Exception as e:
            logger.exception("Failed to launch OpenCode server: %s", e)

        return False

    async def _log_server_output(self, proc: asyncio.subprocess.Process):
        if not proc.stdout:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.debug("[opencode serve] %s", text)

    def add_event_listener(self, listener: Callable[[Dict[str, Any]], Any]):
        """Register a callback for server events."""
        self._event_listeners.append(listener)

    async def start_event_stream(self):
        """Starts listening to OpenCode SSE /event in background."""
        if self._event_task is None or self._event_task.done():
            self._event_task = asyncio.create_task(self._sse_loop())

    async def _sse_loop(self):
        """Persistent SSE event listener."""
        while not self._is_closing:
            try:
                session = await self.get_http_session()
                event_url = f"{self.base_url}/event"
                logger.info("Connecting to OpenCode SSE stream at %s", event_url)

                async with session.get(event_url, timeout=aiohttp.ClientTimeout(total=0, sock_connect=10, sock_read=0)) as resp:
                    if resp.status != 200:
                        logger.warning("Failed to connect to SSE stream: HTTP %s", resp.status)
                        await asyncio.sleep(3)
                        continue

                    logger.info("Connected to OpenCode event stream.")
                    buffer = ""
                    current_event_type = None

                    async for chunk in resp.content.iter_any():
                        if self._is_closing:
                            break
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n\n" in buffer:
                            message, buffer = buffer.split("\n\n", 1)
                            lines = message.split("\n")
                            data_lines = []
                            for line in lines:
                                line = line.strip("\r")
                                if line.startswith("event:"):
                                    current_event_type = line[6:].strip()
                                elif line.startswith("data:"):
                                    data_lines.append(line[5:].strip())

                            if data_lines:
                                raw_data = "\n".join(data_lines)
                                try:
                                    event_obj = json.loads(raw_data)
                                    # Dispatch to listeners
                                    for cb in self._event_listeners:
                                        try:
                                            res = cb(event_obj)
                                            if asyncio.iscoroutine(res):
                                                asyncio.create_task(res)
                                        except Exception as err:
                                            logger.error("Error in event listener callback: %s", err)
                                except json.JSONDecodeError:
                                    logger.debug("Non-JSON SSE payload: %s", raw_data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._is_closing:
                    logger.warning("OpenCode SSE connection disconnected: %s. Reconnecting in 3s...", e)
                    await asyncio.sleep(3)

    async def create_session(self, title: str = "Telegram Chat", directory: Optional[str] = None) -> Dict[str, Any]:
        """Create a new OpenCode session."""
        session = await self.get_http_session()
        payload = {"title": title}
        url = f"{self.base_url}/session"
        if directory:
            url += f"?directory={urllib.parse.quote(directory)}"
        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 201):
                err = await resp.text()
                raise RuntimeError(f"Failed to create session: HTTP {resp.status} - {err}")
            return await resp.json()

    async def list_sessions(self, directory: Optional[str] = None) -> list:
        """List all sessions."""
        session = await self.get_http_session()
        url = f"{self.base_url}/session"
        if directory:
            url += f"?directory={urllib.parse.quote(directory)}"
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            return []

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """Get session details."""
        session = await self.get_http_session()
        async with session.get(f"{self.base_url}/session/{session_id}") as resp:
            if resp.status == 200:
                return await resp.json()
            err = await resp.text()
            raise RuntimeError(f"Failed to get session {session_id}: HTTP {resp.status} - {err}")

    async def get_providers(self) -> Dict[str, Any]:
        """Fetch configured providers and models."""
        session = await self.get_http_session()
        async with session.get(f"{self.base_url}/config/providers") as resp:
            if resp.status == 200:
                return await resp.json()
            return {}

    async def get_diff(self, session_id: str, directory: Optional[str] = None) -> list:
        """Fetch git-style file diffs for the session."""
        session = await self.get_http_session()
        url = f"{self.base_url}/session/{session_id}/diff"
        if directory:
            url += f"?directory={urllib.parse.quote(directory)}"
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def get_todo(self, session_id: str) -> list:
        """Fetch tasks and todo checklist for the session."""
        session = await self.get_http_session()
        async with session.get(f"{self.base_url}/session/{session_id}/todo") as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def find_files(self, query: str, directory: Optional[str] = None) -> list:
        """Find files matching query in workspace."""
        session = await self.get_http_session()
        params = {"query": query}
        if directory:
            params["directory"] = directory
        async with session.get(f"{self.base_url}/find/file", params=params) as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def get_file_content(self, path: str, directory: Optional[str] = None) -> Optional[str]:
        """Read content of a file in the workspace."""
        session = await self.get_http_session()
        params = {"path": path}
        if directory:
            params["directory"] = directory
        async with session.get(f"{self.base_url}/file/content", params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("content", "")
        return None

    async def run_shell(self, session_id: str, command: str, directory: Optional[str] = None) -> Dict[str, Any]:
        """Execute a shell command via OpenCode."""
        session = await self.get_http_session()
        payload = {"agent": "build", "command": command}
        url = f"{self.base_url}/session/{session_id}/shell"
        if directory:
            url += f"?directory={urllib.parse.quote(directory)}"
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                return await resp.json()
            err = await resp.text()
            raise RuntimeError(f"Shell execution failed: {resp.status} {err}")

    async def send_prompt(
        self,
        session_id: str,
        prompt_text: Optional[str] = None,
        model_provider: Optional[str] = None,
        model_id: Optional[str] = None,
        agent: Optional[str] = None,
        directory: Optional[str] = None,
        system: Optional[str] = None,
        prompt: Optional[str] = None,
        **kwargs
    ) -> None:
        """Send a prompt / instruction to the OpenCode session."""
        session = await self.get_http_session()
        actual_text = prompt_text or prompt or ""
        payload: Dict[str, Any] = {
            "parts": [
                {
                    "type": "text",
                    "text": actual_text
                }
            ]
        }
        if agent:
            payload["agent"] = agent
        if system:
            payload["system"] = system
        if model_provider and model_id:
            payload["model"] = {
                "providerID": model_provider,
                "modelID": model_id
            }

        url = f"{self.base_url}/session/{session_id}/prompt_async"
        if directory:
            url += f"?directory={urllib.parse.quote(directory)}"

        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 204):
                err = await resp.text()
                raise RuntimeError(f"Failed to submit prompt: HTTP {resp.status} - {err}")

    async def get_pending_permissions(self) -> list:
        """Fetch all currently pending permissions from OpenCode."""
        try:
            session = await self.get_http_session()
            async with session.get(f"{self.base_url}/permission") as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.warning("Failed to fetch pending permissions: %s", e)
        return []

    async def respond_permission(self, session_id: str, permission_id: str, response: str) -> bool:
        """
        Respond to a permission confirmation request.
        response must be: 'once' | 'always' | 'reject'
        """
        if response not in ("once", "always", "reject"):
            raise ValueError(f"Invalid response type '{response}'. Must be 'once', 'always', or 'reject'.")

        session = await self.get_http_session()
        logger.info("Responding to permission %s (session %s) with '%s'", permission_id, session_id, response)

        # 1. Primary endpoint: /permission/{permission_id}/reply
        try:
            async with session.post(
                f"{self.base_url}/permission/{permission_id}/reply",
                json={"reply": response}
            ) as resp:
                if resp.status in (200, 204):
                    return True
                err = await resp.text()
                logger.debug("/permission reply response: %s %s", resp.status, err)
        except Exception as e:
            logger.warning("Error calling /permission/%s/reply: %s", permission_id, e)

        # 2. Secondary endpoint: /session/{session_id}/permissions/{permission_id}
        if session_id:
            try:
                url = f"{self.base_url}/session/{session_id}/permissions/{permission_id}"
                async with session.post(url, json={"response": response}) as resp:
                    if resp.status in (200, 204):
                        return True
                    err = await resp.text()
                    logger.debug("/session permissions response: %s %s", resp.status, err)
            except Exception as e:
                logger.warning("Error calling %s: %s", url, e)

        return False

    async def switch_session_model(self, session_id: str, provider_id: str, model_id: str) -> bool:
        """Switch model for the session."""
        session = await self.get_http_session()
        payload = {"model": {"id": model_id, "providerID": provider_id}}
        async with session.post(f"{self.base_url}/api/session/{session_id}/model", json=payload) as resp:
            return resp.status in (200, 204)

    async def list_agents(self) -> list:
        """List all visible agents in OpenCode."""
        session = await self.get_http_session()
        async with session.get(f"{self.base_url}/agent") as resp:
            if resp.status == 200:
                agents = await resp.json()
                return [a for a in agents if not a.get("hidden")]
        return []

    async def get_vcs_status(self, directory: Optional[str] = None) -> list:
        """Get git status of working directory."""
        session = await self.get_http_session()
        params = {}
        if directory:
            params["directory"] = directory
        async with session.get(f"{self.base_url}/vcs/status", params=params) as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def get_vcs_diff(self, mode: str = "git", directory: Optional[str] = None) -> list:
        """Get git diff of current repository."""
        session = await self.get_http_session()
        params = {"mode": mode}
        if directory:
            params["directory"] = directory
        async with session.get(f"{self.base_url}/vcs/diff", params=params) as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def share_session(self, session_id: str) -> Optional[str]:
        """Share a session and return public web link."""
        session = await self.get_http_session()
        async with session.post(f"{self.base_url}/session/{session_id}/share") as resp:
            if resp.status == 200:
                data = await resp.json()
                share_info = data.get("share")
                if isinstance(share_info, dict):
                    return share_info.get("url")
        return None

    async def unshare_session(self, session_id: str) -> bool:
        """Unshare a session."""
        session = await self.get_http_session()
        async with session.delete(f"{self.base_url}/session/{session_id}/share") as resp:
            return resp.status in (200, 204)

    async def fork_session(self, session_id: str) -> Optional[str]:
        """Fork an existing session into a new branch."""
        session = await self.get_http_session()
        async with session.post(f"{self.base_url}/session/{session_id}/fork") as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return data.get("id")
        return None

    async def revert_session(self, session_id: str) -> bool:
        """Revert the last message/change in session."""
        session = await self.get_http_session()
        async with session.post(f"{self.base_url}/session/{session_id}/revert") as resp:
            return resp.status in (200, 204)

    async def unrevert_session(self, session_id: str) -> bool:
        """Redo previously reverted changes in session."""
        session = await self.get_http_session()
        async with session.post(f"{self.base_url}/session/{session_id}/unrevert") as resp:
            return resp.status in (200, 204)

    async def find_symbols(self, query: str) -> list:
        """Search code symbols (functions, classes, etc.) via LSP."""
        session = await self.get_http_session()
        async with session.get(f"{self.base_url}/find/symbol", params={"query": query}) as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def list_skills(self) -> list:
        """List active skills in OpenCode."""
        session = await self.get_http_session()
        async with session.get(f"{self.base_url}/skill") as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    async def get_pending_questions(self) -> list:
        """Fetch all currently pending questions from OpenCode."""
        try:
            session = await self.get_http_session()
            async with session.get(f"{self.base_url}/question") as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.warning("Failed to fetch questions: %s", e)
        return []

    async def reply_question(self, request_id: str, answers: list) -> bool:
        """Reply to an interactive question from OpenCode."""
        session = await self.get_http_session()
        payload = {"answers": answers}
        async with session.post(f"{self.base_url}/question/{request_id}/reply", json=payload) as resp:
            return resp.status in (200, 204)

    async def reject_question(self, request_id: str) -> bool:
        """Reject an interactive question from OpenCode."""
        session = await self.get_http_session()
        async with session.post(f"{self.base_url}/question/{request_id}/reject") as resp:
            return resp.status in (200, 204)

    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        """Abort/interrupt current execution in the given session."""
        session = await self.get_http_session()
        params = {}
        if directory:
            params["directory"] = directory
        async with session.post(f"{self.base_url}/session/{session_id}/abort", params=params) as resp:
            return resp.status in (200, 204)

    async def close(self):
        """Clean shutdown."""
        self._is_closing = True
        if self._event_task:
            self._event_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
        if self.server_process:
            try:
                self.server_process.terminate()
                await asyncio.wait_for(self.server_process.wait(), timeout=3)
            except Exception:
                self.server_process.kill()
