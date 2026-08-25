"""Unattended real-browser evaluator for the canonical GreenBook stack.

The evaluator deliberately submits every user turn through the open Frontend
textarea and button.  It uses the browser's same-origin fetch only for
read-only run/message polling and for creating a fresh conversation, because
the current AgentPanel has no visible "new conversation" control.  HITL
actions are always DOM button clicks.

This is an evaluation harness, not production runtime code.  It writes only
JSONL evidence under .runtime/stable-baseline/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets


ROOT = Path(__file__).resolve().parents[2]
CDP_LIST = "http://127.0.0.1:9222/json/list"
TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
WAITING = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}
# Durable approval state and the Frontend Activity subscription are separate
# projections.  Keep observing a pending approval long enough for the
# existing SPA hydration/polling path to render its real card.
APPROVAL_HYDRATION_GRACE_SECONDS = 30.0
COMPOSER_HYDRATION_GRACE_SECONDS = 30.0
RAW_LEAK_RE = re.compile(
    r"(?i)(?:operation[_ ]?id|execution[_ ]?id|objective[_ ]?id|authorization|bearer\s+ey|jwt|result_unknown)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(value: Any, *, depth: int = 0) -> Any:
    """Remove credentials and bound very large values before evidence writes."""

    if depth > 8:
        return "<depth-limit>"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("access_token", "refresh_token", "authorization", "password")):
                output[str(key)] = "<redacted>"
            else:
                output[str(key)] = compact(item, depth=depth + 1)
        return output
    if isinstance(value, list):
        return [compact(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str) and len(value) > 12000:
        return value[:12000] + "<truncated>"
    return value


class Browser:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self.ws: websockets.ClientConnection | None = None
        self._message_id = 0

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.ws_url, max_size=16_000_000)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def command(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.ws is None:
            raise RuntimeError("CDP is not connected")
        self._message_id += 1
        message_id = self._message_id
        await self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = await self.ws.recv()
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method}: {message['error']}")
                return message.get("result")

    async def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }
        for attempt in range(2):
            try:
                result = await self.command("Runtime.evaluate", params)
                break
            except RuntimeError as exc:
                # A real SPA navigation can replace the inspected execution
                # context and, on Chromium, occasionally the CDP page target
                # itself.  Re-acquire only the current Frontend page; never
                # retry the business submission or any product-side action.
                if attempt or not re.search(r"Inspected target navigated or closed", str(exc)):
                    raise
                await self.close()
                self.ws_url = find_page()
                await self.connect()
        else:  # pragma: no cover - the loop either returns or raises
            raise RuntimeError("CDP evaluation retry exhausted")
        remote = (result or {}).get("result", {})
        if remote.get("subtype") == "error" or remote.get("type") == "error":
            raise RuntimeError(remote.get("description") or "browser evaluation error")
        if "value" in remote:
            return remote["value"]
        return None

    async def prefer_conversation_on_next_document(self, conversation_id: str) -> str:
        """Install a bounded conversation ordering wrapper before reload.

        AgentPanel selects its first conversation during the initial React
        mount. A preference installed only on the current document is lost by
        ``location.reload()``, so a resumed checkpoint can render a different
        conversation even while the read-only API projection is correct.
        """

        source = f"""(() => {{
          const preferred = {json.dumps(conversation_id)};
          window.__greenbookPreferredConversationId = preferred;
          if (window.__greenbookConversationFetchPatched) return;
          const nativeFetch = window.fetch.bind(window);
          window.fetch = async (...args) => {{
            const request = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
            const method = String(args[1]?.method || args[0]?.method || 'GET').toUpperCase();
            const path = request.split('?')[0];
            if (method === 'GET' && path.endsWith('/api/v1/agent/conversations')) {{
              try {{
                const url = new URL(request, location.origin);
                url.searchParams.set('page', '1');
                url.searchParams.set('size', '100');
                const response = await nativeFetch(url.toString(), args[1] || {{}});
                const payload = await response.clone().json();
                let list = Array.isArray(payload) ? payload : payload?.items;
                if (Array.isArray(list)) {{
                  list = [...list].sort((left, right) =>
                    (left.conversation_id === preferred ? -1 : 0)
                    - (right.conversation_id === preferred ? -1 : 0)
                  );
                  const body = Array.isArray(payload) ? list : {{ ...payload, items: list }};
                  return new Response(JSON.stringify(body), {{
                    status: response.status,
                    statusText: response.statusText,
                    headers: response.headers
                  }});
                }}
              }} catch {{}}
            }}
            return nativeFetch(...args);
          }};
          window.__greenbookConversationFetchPatched = true;
        }})();"""
        result = await self.command(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source},
        )
        return str((result or {}).get("identifier") or "")

    async def api(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        body_js = "undefined" if body is None else json.dumps(body, ensure_ascii=False)
        expression = f"""(async()=>{{
          const raw=JSON.parse(localStorage.getItem('zhiguang_auth_tokens')||'{{}}');
          const options={{method:{json.dumps(method)},headers:{{Authorization:'Bearer '+(raw.accessToken||''),'Content-Type':'application/json'}}}};
          if ({body_js} !== undefined) options.body=JSON.stringify({body_js});
          const controller=new AbortController();
          const timer=setTimeout(()=>controller.abort(),45000);
          options.signal=controller.signal;
          let response;
          try{{response=await fetch({json.dumps('/agent-api' + path)},options);}}
          catch(error){{return {{status:599,data:null,text:String(error&&error.message||error)}};}}
          finally{{clearTimeout(timer);}}
          const text=await response.text(); let data=null; try{{data=text?JSON.parse(text):null;}}catch{{data=null;}}
          return {{status:response.status,data,text:text.slice(0,3000)}};
        }})()"""
        value = await self.evaluate(expression)
        if not isinstance(value, dict):
            raise RuntimeError(f"invalid API response for {method} {path}")
        return value

    async def snapshot(self) -> dict[str, Any]:
        value = await self.evaluate(
            """(()=>({
              url:location.href,
              body:document.body.innerText.slice(-8000),
              textareas:[...document.querySelectorAll('textarea')].map(x=>({value:x.value,placeholder:x.placeholder})),
              candidates:[...document.querySelectorAll('section[class*="clarificationCard"]')].flatMap(s=>[...s.querySelectorAll('button')].map((b,i)=>({i,text:b.innerText,disabled:b.disabled}))),
              semantic_confirmations:[...document.querySelectorAll('article')]
                .filter(a=>!a.matches('[aria-label="需要你的确认"]')
                  && [...a.querySelectorAll('button')].some(b=>/确认执行|确认发布/.test(String(b.innerText||''))))
                .map(a=>({
                  title:(a.querySelector('h3')?.innerText||'').trim(),
                  buttons:[...a.querySelectorAll('button')].map(b=>({text:(b.innerText||'').trim(),disabled:b.disabled}))
                })),
              approvals:[...document.querySelectorAll('[class]')].filter(a=>/approvalActions/i.test(String(a.className))).map(a=>[...a.querySelectorAll('button')].map(b=>({text:b.innerText,disabled:b.disabled}))),
              buttons:[...document.querySelectorAll('button')].slice(-30).map(b=>({text:b.innerText,aria:b.getAttribute('aria-label'),class:String(b.className),disabled:b.disabled}))
            }))()"""
        )
        return value if isinstance(value, dict) else {}

    async def panel_open(self) -> bool:
        return bool(
            await self.evaluate(
                "Boolean(document.querySelector('textarea[name=\"agent-message\"]') || document.querySelector('textarea[aria-label*=\"Agent\"]'))"
            )
        )

    async def open_panel(self) -> None:
        if await self.panel_open():
            return
        await self.evaluate("document.querySelector('button[class*=agentTrigger]')?.click(); true")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if await self.panel_open():
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("AgentPanel did not open")

    async def close_panel(self) -> None:
        if not await self.panel_open():
            return
        await self.evaluate(
            """(()=>{
              const buttons=[...document.querySelectorAll('button')];
              const close=buttons.find(b=>String(b.getAttribute('aria-label')||'').length>0
                && String(b.className).includes('iconButton')
                && !String(b.className).includes('agentTrigger'));
              close?.click(); return Boolean(close);
            })()"""
        )
        await asyncio.sleep(0.25)

    async def new_conversation(self, title: str) -> str:
        response = await self.api("POST", "/api/v1/agent/conversations", {"title": title, "surface": "HOME"})
        if response.get("status") != 200:
            raise RuntimeError(f"conversation create failed: {response}")
        data = response.get("data") or {}
        conversation_id = str(data.get("conversation_id") or "")
        if not conversation_id:
            raise RuntimeError(f"conversation id missing: {response}")
        # AgentPanel currently restores the first conversation returned by its
        # existing list endpoint.  Keep the browser on the same real Panel
        # initialization path, but make the newly-created conversation first
        # for this evaluation session so a case cannot inherit another case's
        # context.  This only wraps the browser page's read-only list fetch;
        # message submission and all HITL actions still go through the UI.
        await self.evaluate(
            f"""(()=>{{
              const preferred={json.dumps(conversation_id)};
              window.__greenbookPreferredConversationId=preferred;
              if(!window.__greenbookConversationFetchPatched){{
                const nativeFetch=window.fetch.bind(window);
                window.fetch=async (...args)=>{{
                  const response=await nativeFetch(...args);
                  const request=typeof args[0]==='string'?args[0]:(args[0]?.url||'');
                  const method=String(args[1]?.method||args[0]?.method||'GET').toUpperCase();
                  const path=request.split('?')[0];
                   if(method==='GET' && path.endsWith('/api/v1/agent/conversations')){{
                     try{{
                       const payload=await response.clone().json();
                       let list=Array.isArray(payload)?payload:payload?.items;
                       const id=window.__greenbookPreferredConversationId;
                       // The canonical endpoint defaults to the newest 20
                       // conversations.  A resumed checkpoint may be older
                       // than that page; boundedly expand the same projection
                       // so the real AgentPanel can select it.
                       if(Array.isArray(list) && id && !list.some(item=>item?.conversation_id===id)){{
                         const expandedUrl=new URL(request,location.origin);
                         expandedUrl.searchParams.set('page','1');
                         expandedUrl.searchParams.set('size','100');
                         const expandedResponse=await nativeFetch(expandedUrl.toString(),args[1]||{{}});
                         const expandedPayload=await expandedResponse.clone().json();
                         const expandedList=Array.isArray(expandedPayload)?expandedPayload:expandedPayload?.items;
                         if(Array.isArray(expandedList)) list=expandedList;
                       }}
                       if(Array.isArray(list)){{
                         list.sort((left,right)=>(left.conversation_id===id?-1:0)-(right.conversation_id===id?-1:0));
                         const body=Array.isArray(payload)?list:{{...payload,items:list}};
                         return new Response(JSON.stringify(body),{{status:response.status,statusText:response.statusText,headers:response.headers}});
                      }}
                    }}catch{{}}
                  }}
                  return response;
                }};
                window.__greenbookConversationFetchPatched=true;
              }}
              return true;
            }})()"""
        )
        # A preceding View case may have navigated the real SPA to /post/:id.
        # Return through browser navigation before opening the next Agent
        # conversation; otherwise the post page has no HOME composer target.
        current_path = str(await self.evaluate("location.pathname") or "")
        if current_path != "/":
            await self.evaluate("location.href='/'; true")
            navigation_deadline = time.monotonic() + 8.0
            while time.monotonic() < navigation_deadline:
                try:
                    state = await self.evaluate(
                        "({path:location.pathname,ready:document.readyState})"
                    )
                    if (
                        isinstance(state, dict)
                        and state.get("path") == "/"
                        and state.get("ready") in {"interactive", "complete"}
                    ):
                        break
                except Exception:
                    # Chromium may briefly discard the old execution context
                    # while the new document is being committed.
                    pass
                await asyncio.sleep(0.2)
        await asyncio.sleep(0.3)
        # A navigation destroys page globals and the fetch wrapper above.  The
        # new conversation must be preferred after navigation, before the
        # AgentPanel is mounted again; otherwise a post-page start can send
        # the next UI turn to an older conversation while the harness polls
        # the newly-created one.
        await self.evaluate(
            f"""(()=>{{
              const preferred={json.dumps(conversation_id)};
              window.__greenbookPreferredConversationId=preferred;
              if(!window.__greenbookConversationFetchPatched){{
                const nativeFetch=window.fetch.bind(window);
                window.fetch=async (...args)=>{{
                  const response=await nativeFetch(...args);
                  const request=typeof args[0]==='string'?args[0]:(args[0]?.url||'');
                  const method=String(args[1]?.method||args[0]?.method||'GET').toUpperCase();
                  const path=request.split('?')[0];
                  if(method==='GET' && path.endsWith('/api/v1/agent/conversations')){{
                    try{{
                      const payload=await response.clone().json();
                      const list=Array.isArray(payload)?payload:payload?.items;
                      if(Array.isArray(list)){{
                        const id=window.__greenbookPreferredConversationId;
                        list.sort((left,right)=>(left.conversation_id===id?-1:0)-(right.conversation_id===id?-1:0));
                        const body=Array.isArray(payload)?list:{{...payload,items:list}};
                        return new Response(JSON.stringify(body),{{status:response.status,statusText:response.statusText,headers:response.headers}});
                      }}
                    }}catch{{}}
                  }}
                  return response;
                }};
                window.__greenbookConversationFetchPatched=true;
              }}
              return true;
            }})()"""
        )
        await self.close_panel()
        await self.open_panel()
        return conversation_id

    async def prefer_conversation(self, conversation_id: str) -> None:
        """Select an existing conversation through the normal AgentPanel list."""

        hydration_deadline = time.monotonic() + COMPOSER_HYDRATION_GRACE_SECONDS
        while time.monotonic() < hydration_deadline:
            state = await self.evaluate(
                "Boolean(document.querySelector('textarea[name=\"agent-message\"]') && !document.querySelector('textarea[name=\"agent-message\"]').disabled)"
            )
            if state:
                break
            await asyncio.sleep(0.25)
        await self.evaluate(
            f"""(()=>{{
              const preferred={json.dumps(conversation_id)};
              window.__greenbookPreferredConversationId=preferred;
              if(!window.__greenbookConversationFetchPatched){{
                const nativeFetch=window.fetch.bind(window);
                window.fetch=async (...args)=>{{
                  const response=await nativeFetch(...args);
                  const request=typeof args[0]==='string'?args[0]:(args[0]?.url||'');
                  const method=String(args[1]?.method||args[0]?.method||'GET').toUpperCase();
                  const path=request.split('?')[0];
                  if(method==='GET' && path.endsWith('/api/v1/agent/conversations')){{
                    try{{
                      const payload=await response.clone().json();
                      let list=Array.isArray(payload)?payload:payload?.items;
                      const id=window.__greenbookPreferredConversationId;
                      // The canonical endpoint defaults to the newest 20
                      // conversations. A resumed checkpoint may be older
                      // than that page; expand the same bounded projection
                      // so the real AgentPanel can select it.
                      if(Array.isArray(list) && id && !list.some(item=>item?.conversation_id===id)){{
                        const expandedUrl=new URL(request,location.origin);
                        expandedUrl.searchParams.set('page','1');
                        expandedUrl.searchParams.set('size','100');
                        const expandedResponse=await nativeFetch(expandedUrl.toString(),args[1]||{{}});
                        const expandedPayload=await expandedResponse.clone().json();
                        const expandedList=Array.isArray(expandedPayload)?expandedPayload:expandedPayload?.items;
                        if(Array.isArray(expandedList)) list=expandedList;
                      }}
                      if(Array.isArray(list)){{
                        list.sort((left,right)=>(left.conversation_id===id?-1:0)-(right.conversation_id===id?-1:0));
                        const body=Array.isArray(payload)?list:{{...payload,items:list}};
                        return new Response(JSON.stringify(body),{{status:response.status,statusText:response.statusText,headers:response.headers}});
                      }}
                    }}catch{{}}
                  }}
                  return response;
                }};
                window.__greenbookConversationFetchPatched=true;
              }}
              return true;
            }})()"""
        )
        await self.close_panel()
        await self.open_panel()
        hydration_deadline = time.monotonic() + COMPOSER_HYDRATION_GRACE_SECONDS
        while time.monotonic() < hydration_deadline:
            state = await self.evaluate(
                "Boolean(document.querySelector('textarea[name=\"agent-message\"]') && !document.querySelector('textarea[name=\"agent-message\"]').disabled)"
            )
            if state:
                return
            await asyncio.sleep(0.25)

    async def send_ui(self, message: str) -> None:
        # Use the browser input surface so React's controlled textarea state
        # receives the same input event as a real user.  Assigning the DOM
        # value directly can leave the visible value changed while React's
        # `content` state remains empty, which disables Send and parks the
        # harness before any product Run is admitted.  The message originates
        # in a strict UTF-8 fixture; CDP transports the Unicode text without
        # a PowerShell code-page round trip.
        composer_deadline = time.monotonic() + COMPOSER_HYDRATION_GRACE_SECONDS
        while time.monotonic() < composer_deadline:
            ready = await self.evaluate(
                "Boolean(document.querySelector('textarea[name=\"agent-message\"]') && !document.querySelector('textarea[name=\"agent-message\"]').disabled)"
            )
            if ready:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("AgentPanel composer remained locked during hydration")
        result = await self.evaluate(
            """(()=>{
              const textarea=document.querySelector('textarea[name="agent-message"]')
                || document.querySelector('textarea[aria-label*="Agent"]');
              if(!textarea) return {ok:false,error:'textarea missing'};
              textarea.focus(); textarea.select();
              return {ok:true};
            })()"""
        )
        if not result or not result.get("ok"):
            raise RuntimeError(f"failed to focus AgentPanel: {result}")
        await self.command("Input.insertText", {"text": message})
        # React may commit the controlled textarea a few frames after CDP's
        # Input.insertText returns, especially immediately after reopening the
        # AgentPanel for a fresh conversation.  Wait for the real visible
        # value and enabled Send button; do not click or submit while the UI
        # still represents an empty composer.
        input_deadline = time.monotonic() + COMPOSER_HYDRATION_GRACE_SECONDS
        input_state: dict[str, Any] | None = None
        while time.monotonic() < input_deadline:
            input_state = await self.evaluate(
                """(()=>{
                  const textarea=document.querySelector('textarea[name="agent-message"]')
                    || document.querySelector('textarea[aria-label*="Agent"]');
                  const send=[...document.querySelectorAll('button')]
                    .find(item=>item.getAttribute('aria-label')==='发送');
                  return {value:textarea?.value||'',sendDisabled:send?.disabled!==false};
                })()"""
            )
            if (
                isinstance(input_state, dict)
                and str(input_state.get("value") or "") == message
                and not bool(input_state.get("sendDisabled"))
            ):
                break
            await asyncio.sleep(0.1)
        if not (
            isinstance(input_state, dict)
            and str(input_state.get("value") or "") == message
            and not bool(input_state.get("sendDisabled"))
        ):
            raise RuntimeError(f"AgentPanel did not accept visible input: {input_state}")
        clicked = await self.evaluate(
            """(()=> {
              const sendLabel=String.fromCodePoint(0x53d1,0x9001);
              const buttons=[...document.querySelectorAll('button')];
              const button=buttons.find(item=>item.getAttribute('aria-label')===sendLabel)
                || buttons.find(item=>item.getAttribute('aria-label') && !item.disabled
                  && item.closest('[role="dialog"]'))
                || buttons.at(-1);
              if(!button||button.disabled)return {ok:false};
              button.click();return {ok:true};
            })()"""
        )
        if not clicked or not clicked.get("ok"):
            raise RuntimeError(f"failed to click Agent send: {clicked}")

    async def list_runs(self, conversation_id: str) -> list[dict[str, Any]]:
        response = await self.api("GET", "/api/v1/agent/runs?limit=100")
        if response.get("status") != 200:
            return []
        data = response.get("data")
        if isinstance(data, dict):
            data = data.get("items", [])
        return [item for item in (data or []) if str(item.get("conversation_id") or "") == conversation_id]

    async def list_conversations(self, context_post_id: str | None = None) -> list[dict[str, Any]]:
        path = "/api/v1/agent/conversations"
        if context_post_id:
            path += f"?context_post_id={context_post_id}"
        response = await self.api("GET", path)
        if response.get("status") != 200:
            return []
        data = response.get("data")
        if isinstance(data, dict):
            data = data.get("items", [])
        return list(data or [])

    async def get_run(self, run_id: str) -> dict[str, Any]:
        response = await self.api("GET", f"/api/v1/agent/runs/{run_id}")
        return response.get("data") or {}

    async def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        response = await self.api("GET", f"/api/v1/agent/conversations/{conversation_id}/messages")
        data = response.get("data")
        return data if isinstance(data, list) else []

    async def click_hitl(self, policy: dict[str, Any]) -> dict[str, Any] | None:
        action = str(policy.get("hitl") or "")
        if action == "clarify":
            index = int(policy.get("candidate_index", 0))
            return await self.evaluate(
                f"""(()=>{{const groups=[...document.querySelectorAll('section[class*="clarificationCard"]')];const buttons=groups.flatMap(g=>[...g.querySelectorAll('button')]);const b=buttons[{index}];if(!b||b.disabled)return {{clicked:false,count:buttons.length}};b.click();return {{clicked:true,count:buttons.length,index:{index},label:b.innerText}};}})()"""
            )
        if action in {"approve", "reject"}:
            if action == "approve":
                selector = """(()=>{const groups=[...document.querySelectorAll('[class]')].filter(a=>/approvalActions/i.test(String(a.className)));const g=groups.at(-1);const bs=g?[...g.querySelectorAll('button')]:[];const labels=new Set(['\\u786e\\u8ba4\\u6267\\u884c','\\u786e\\u8ba4\\u53d1\\u5e03','\\u786e\\u8ba4\\u5220\\u9664']);const b=bs.find(item=>labels.has(String(item.innerText||'').trim()))||null;if(!b||b.disabled)return {clicked:false,count:bs.length};b.click();return {clicked:true,label:b.innerText};})()"""
            else:
                selector = """(()=>{const groups=[...document.querySelectorAll('[class]')].filter(a=>/approvalActions/i.test(String(a.className)));const g=groups.at(-1);const bs=g?[...g.querySelectorAll('button')]:[];const labels=new Set(['\\u62d2\\u7edd','\\u6682\\u4e0d\\u6267\\u884c','\\u53d6\\u6d88','\\u786e\\u8ba4\\u53d6\\u6d88']);const b=bs.find(item=>labels.has(String(item.innerText||'').trim()))||null;if(!b||b.disabled)return {clicked:false,count:bs.length};b.click();return {clicked:true,label:b.innerText};})()"""
            return await self.evaluate(selector)
        if action in {"confirm", "cancel", "modify"}:
            # SemanticConfirmationCard is the only user-facing confirmation
            # surface for semantic confirmation.  Click the visible DOM
            # button by its rendered label; never call the internal API from
            # this harness.  Approval cards are explicitly excluded.
            label = "确认执行" if action == "confirm" else "取消"
            if action == "modify":
                label = "修改安排"
            return await self.evaluate(
                f"""(()=>{{const cards=[...document.querySelectorAll('article')].filter(a=>!a.matches('[aria-label="需要你的确认"]')&&[...a.querySelectorAll('button')].some(b=>/确认执行|确认发布|修改安排|取消/.test(String(b.innerText||''))));const card=cards.at(-1);const bs=card?[...card.querySelectorAll('button')]:[];const b=bs.find(item=>String(item.innerText||'').trim()==={json.dumps(label,ensure_ascii=False)})||null;if(!b||b.disabled)return {{clicked:false,count:bs.length,expected:{json.dumps(label,ensure_ascii=False)}}};b.click();return {{clicked:true,label:b.innerText,surface:'frontend-semantic-confirmation'}};}})()"""
            )
        return None


def find_page() -> str:
    pages = json.load(urllib.request.urlopen(CDP_LIST))
    for page in pages:
        if page.get("type") == "page" and str(page.get("url") or "").startswith("http://127.0.0.1:5173"):
            return str(page["webSocketDebuggerUrl"])
    raise RuntimeError("no Frontend page available on CDP")


def newest_run(runs: list[dict[str, Any]], *, after: float | None = None) -> dict[str, Any] | None:
    candidates = [
        item for item in runs
        if isinstance(item, dict) and _run_id(item)
    ]
    if not candidates:
        return None
    # ISO timestamps are lexically sortable and all runtime timestamps are UTC.
    return sorted(candidates, key=_run_created_at)[-1]


def _run_id(item: Any) -> str:
    """Read only a durable Run identity from a Browser/API projection."""

    if not isinstance(item, dict):
        return ""
    value = str(item.get("run_id") or "").strip()
    if value:
        return value
    for key in ("run", "data", "projection", "payload"):
        nested = item.get(key)
        if isinstance(nested, dict):
            value = str(nested.get("run_id") or "").strip()
            if value:
                return value
    return ""


def _run_created_at(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    value = str(item.get("created_at") or "").strip()
    if value:
        return value
    for key in ("run", "data", "projection", "payload"):
        nested = item.get(key)
        if isinstance(nested, dict):
            value = str(nested.get("created_at") or "").strip()
            if value:
                return value
    return ""


async def wait_for_new_run(
    browser: Browser,
    conversation_id: str,
    deadline: float,
    *,
    before: set[str] | None = None,
) -> dict[str, Any]:
    """Wait for the run admitted by this UI turn, never an older latest run."""
    previous = before or set()
    while time.monotonic() < deadline:
        runs = await browser.list_runs(conversation_id)
        candidates = [
            item
            for item in runs
            if _run_id(item)
            and _run_id(item) not in previous
        ]
        run = newest_run(candidates)
        if run:
            return run
        await asyncio.sleep(0.5)
    raise TimeoutError("run was not accepted by Agent API")


async def run_turn(
    browser: Browser,
    conversation_id: str,
    text: str,
    policy: dict[str, Any],
    timeout: float,
    *,
    existing_run_id: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if existing_run_id:
        # Resume an already-admitted durable Run.  This path is observation
        # plus real DOM HITL only; it must never submit the natural-language
        # action again.
        await browser.open_panel()
        current = await browser.get_run(existing_run_id)
    else:
        # A search-card click is a real navigation to /post/:id.  Restore the
        # existing Home composer before the next user turn so the evaluator
        # sends through the same AgentPanel UI instead of polling the previous
        # run.
        pathname = str(await browser.evaluate("location.pathname") or "")
        if pathname.startswith("/post/"):
            # CourseDetailPage owns a POST-scoped AgentPanel.  Open that
            # existing UI surface and follow its context-bound conversation
            # after a real search-card navigation.
            if not await browser.panel_open():
                await browser.evaluate("document.querySelector('button[class*=agentTrigger]')?.click(); true")
                await asyncio.sleep(0.3)
            await browser.open_panel()
            post_id = pathname.rsplit("/", 1)[-1]
            contextual = []
            for _ in range(20):
                contextual = await browser.list_conversations(post_id)
                if contextual:
                    conversation_id = str(contextual[0].get("conversation_id") or conversation_id)
                    break
                await asyncio.sleep(0.25)
        elif pathname != "/":
            await browser.evaluate("location.href='/' ; true")
            await asyncio.sleep(0.8)
            await browser.open_panel()
        before = {
            _run_id(item)
            for item in await browser.list_runs(conversation_id)
            if _run_id(item)
        }
        await browser.send_ui(text)
        # Queue admission can legitimately take longer than the UI's
        # optimistic acknowledgement on a cold in-process consumer.  Keep the
        # acceptance poll long enough to observe a real terminal Chat/Query
        # run instead of turning that scheduling delay into a harness error.
        # Complex multi-objective semantic admission can spend over a minute in
        # the existing provider/normalization boundary before its Run
        # projection is visible.  Keep that admission delay out of the product
        # result.
        current = await wait_for_new_run(
            browser,
            conversation_id,
            started + float(timeout),
            before=before,
        )
    last_known_run_id = _run_id(current)
    if not last_known_run_id:
        raise RuntimeError("durable run identity missing after admission")

    clicked: list[dict[str, Any]] = []
    seen_waiting: set[str] = set()
    waiting_polls: dict[str, int] = {}
    deadline = started + timeout
    while time.monotonic() < deadline:
        run_id = _run_id(current) or last_known_run_id
        if not run_id:
            recovered = newest_run(await browser.list_runs(conversation_id))
            run_id = _run_id(recovered)
            if run_id:
                current = recovered or current
        if not run_id:
            raise RuntimeError("durable run identity missing")
        last_known_run_id = run_id
        current = await browser.get_run(run_id)
        if not _run_id(current):
            current = {**current, "run_id": run_id}
        status = str(current.get("status") or "")
        # A confirmation/approval click may create a continuation Run while
        # the parent remains RUNNING.  Follow it even before the parent turns
        # terminal; otherwise an evaluator can wait on a completed parent
        # boundary and miss the actual child execution.
        if clicked:
            newer_runs = [
                item
                for item in await browser.list_runs(conversation_id)
                if _run_id(item) != run_id
                and _run_id(item)
                and _run_created_at(item) > _run_created_at(current)
            ]
            if newer_runs:
                current = newest_run(newer_runs) or current
                continue
        approval = current.get("approval")
        api_approval_pending = isinstance(approval, dict) and bool(
            approval.get("approval_id")
        ) and str(approval.get("status") or "PENDING").upper() in {
            "PENDING",
            "WAITING",
        }
        approval_pending = api_approval_pending
        # Some approval continuations retain RUNNING in the run projection
        # and publish the actionable state only through the existing Frontend
        # activity card.  The evaluator must observe that real UI contract,
        # otherwise it can park forever even though the user can act.
        ui_snapshot = await browser.snapshot()
        ui_approval_groups = ui_snapshot.get("approvals") or []
        ui_semantic_groups = ui_snapshot.get("semantic_confirmations") or []
        dom_approval_pending = any(
            isinstance(group, list)
            and any(isinstance(button, dict) and not button.get("disabled") for button in group)
            for group in ui_approval_groups
        )
        dom_semantic_pending = any(
            isinstance(group, dict)
            and any(
                isinstance(button, dict)
                and not button.get("disabled")
                and str(button.get("text") or "").strip() in {"确认执行", "确认发布"}
                for button in (group.get("buttons") or [])
            )
            for group in ui_semantic_groups
        )
        approval_pending = approval_pending or dom_approval_pending
        semantic_pending = dom_semantic_pending and str(policy.get("hitl") or "") in {
            "confirm",
            "modify",
            "cancel",
        }
        if status in WAITING or approval_pending or semantic_pending:
            # The API keeps an approval-gated execution in RUNNING while the
            # Frontend renders the real approval card.  Treat the persisted
            # pending approval as HITL evidence instead of waiting for a
            # status transition that cannot happen before the browser click.
            hitl_status = status if status in WAITING else "WAITING_APPROVAL"
            dom_signature = json.dumps(ui_approval_groups, ensure_ascii=True, sort_keys=True)
            key = f"{run_id}:{hitl_status}:{(approval or {}).get('approval_id', '')}:{dom_signature}"
            waiting_polls[key] = waiting_polls.get(key, 0) + 1
            if key not in seen_waiting:
                hitl_sequence = policy.get("hitl_sequence")
                if isinstance(hitl_sequence, list) and hitl_sequence:
                    remaining_actions = hitl_sequence[len(clicked):] or hitl_sequence[-1:]
                else:
                    remaining_actions = [policy.get("hitl")]
                action = None
                for hitl_action in remaining_actions:
                    click_policy = dict(policy)
                    click_policy["hitl"] = hitl_action
                    action = await browser.click_hitl(click_policy)
                    if action and action.get("clicked"):
                        break
                # The run can become WAITING_USER before React has projected
                # the card.  Retry a not-yet-clickable card on the next poll;
                # only suppress future attempts after the real DOM click.
                if action and action.get("clicked"):
                    sequence_finished = not (
                        isinstance(hitl_sequence, list)
                        and len(clicked) + 1 < len(hitl_sequence)
                    )
                    if sequence_finished:
                        seen_waiting.add(key)
                    clicked.append({"status": hitl_status, "action": action, "at": utc_now()})
                    await asyncio.sleep(0.8)
                    candidates = await browser.list_runs(conversation_id)
                    current_created_at = _run_created_at(current)
                    newer = [
                        item for item in candidates
                        if _run_id(item) != run_id
                        and _run_id(item)
                        and _run_created_at(item) > current_created_at
                    ]
                    if newer:
                        current = newest_run(newer) or current
                        continue
            # A plain Chat/Query must not leave an unattended evaluator
            # parked forever.  Give the existing projection a few seconds
            # to render its HITL card, then record the waiting state as a
            # fresh product failure.
            if waiting_polls[key] >= 6:
                # A durable approval can be visible through RunResponse and
                # Activity storage before React receives the SSE/replay poll.
                # Do not turn that bounded hydration window into a false
                # product/harness failure.  We still stop at the normal turn
                # deadline and report the pending state if the card never
                # becomes actionable.
                hydration_deadline = min(
                    deadline,
                    started + APPROVAL_HYDRATION_GRACE_SECONDS,
                )
                if api_approval_pending and time.monotonic() < hydration_deadline:
                    await asyncio.sleep(0.8)
                    continue
                break
        if status in TERMINAL:
            # A HITL continuation can supersede the waiting parent.  Follow
            # the newest same-conversation run if it appeared after the click.
            candidates = await browser.list_runs(conversation_id)
            latest = newest_run(candidates)
            if latest and _run_id(latest) != run_id and clicked:
                latest_status = str(latest.get("status") or "")
                if latest_status not in {""}:
                    current = latest
                    continue
            break
        await asyncio.sleep(0.8)

    elapsed = round(time.monotonic() - started, 3)
    ui_action_result: dict[str, Any] | None = None
    if str(policy.get("ui_action") or "") == "click_search_first":
        index = int(policy.get("search_index", 0))
        ui_action_result = await browser.evaluate(
            f"""(()=>{{
              const links=[...document.querySelectorAll('a[class*="searchLink"]')];
              const recent=links.slice(-5);
              const link=recent[{index}];
              if(!link) return {{clicked:false,count:recent.length}};
              const href=link.getAttribute('href')||'';
              const title=(link.innerText||'').trim();
              link.click();
              return {{clicked:true,count:recent.length,index:{index},href,title}};
            }})()"""
        )
        await asyncio.sleep(1.0)
    snapshot = await browser.snapshot()
    messages = await browser.messages(conversation_id)
    result = {
        "recorded_at": utc_now(),
        "utterance": text,
        "conversation_id": conversation_id,
        "run_id": _run_id(current) or last_known_run_id,
        "status": current.get("status"),
        "error_code": current.get("error_code") or current.get("error"),
        "elapsed_seconds": elapsed,
        "first_feedback_latency": "NOT_INSTRUMENTED",
        "clicked_hitl": clicked,
        "semantic_confirmation_dom_clicks": [
            item for item in clicked
            if (item.get("action") or {}).get("surface") == "frontend-semantic-confirmation"
        ],
        "run": compact(current),
        "message_count": len(messages),
        "last_message": compact(messages[-1] if messages else None),
        "ui": compact(snapshot),
        "ui_action": compact(ui_action_result),
        "ui_internal_leak": bool(RAW_LEAK_RE.search(str(snapshot.get("body") or ""))),
    }
    return result


def built_in_cases(prefix: str) -> list[dict[str, Any]]:
    """Compact broad first-round set; more repetitions can be supplied by JSON."""

    return [
        {
            "name": "chat",
            "turns": [
                {"text": "你好"},
                {"text": "你能做什么"},
                {"text": "谢谢"},
                {"text": "请用一句话解释什么是缓存"},
                {"text": "给我一个与社区业务无关的旅行收纳建议"},
            ],
        },
        {
            "name": "search",
            "turns": [
                {"text": "搜一些 Java 学习相关帖子"},
                {"text": "搜几篇 Agent 学习相关帖子"},
                {"text": "找一些 Redis 缓存实践文章"},
                {"text": "搜索 Spring Boot 入门内容"},
                {"text": "最近有哪些 Python 和 AI 相关帖子"},
                {"text": "搜热门的 Java 面试经验"},
                {"text": "有没有高质量的 Agent 工程实践"},
                {"text": "帮我找 Redis 数据结构介绍"},
                {"text": "找 Spring Boot REST API 教程"},
                {"text": "搜索 Python 初学者内容"},
                {"text": "搜 Java 集合相关文章"},
                {"text": "搜 Agent memory 相关帖子"},
                {"text": "搜 Redis 性能优化相关帖子"},
                {"text": "搜 Spring Boot 最近的内容"},
                {"text": "搜 AI 学习路线帖子"},
            ],
        },
        {
            "name": "create",
            "turns": [
                {"text": f"创建一个标题为 {prefix}-JAVA 的 Java 学习草稿，只保存草稿"},
                {"text": f"创建一个标题为 {prefix}-AGENT 的 Agent 学习草稿，只保存草稿"},
                {"text": f"创建一个标题为 {prefix}-REDIS 的 Redis 学习草稿，只保存草稿"},
                {"text": f"创建一个标题为 {prefix}-SPRING 的 Spring Boot 学习草稿，只保存草稿"},
                {"text": f"创建一个标题为 {prefix}-AI 的 AI 学习草稿，只保存草稿"},
            ],
        },
        {
            "name": "target-clarify",
            "turns": [
                {"text": f"创建一个标题为 {prefix}-CLARIFY-A 的 Java 草稿，只保存草稿"},
                {"text": f"创建一个标题为 {prefix}-CLARIFY-B 的 Java 草稿，只保存草稿"},
                {"text": "删除 Java 学习草稿", "hitl": "clarify", "candidate_index": 0},
            ],
        },
    ]


def load_utf8_cases(path: Path) -> list[dict[str, Any]]:
    """Load evaluation cases from a strict UTF-8 JSON boundary.

    Business text must never pass through a PowerShell console/code page.  A
    strict decode makes a damaged fixture fail before it can submit a turn;
    the browser then receives the already-decoded Python string through CDP.
    """

    payload = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"case file must contain a JSON array: {path}")
    cases: list[dict[str, Any]] = []
    for index, case in enumerate(payload):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} is not an object: {path}")
        for turn_index, turn in enumerate(case.get("turns") or [], start=1):
            if not isinstance(turn, dict) or not isinstance(turn.get("text"), str):
                raise ValueError(f"case {index} turn {turn_index} has no text: {path}")
            if "\ufffd" in turn["text"]:
                raise ValueError(f"case {index} turn {turn_index} contains UTF-8 replacement character: {path}")
        cases.append(case)
    return cases


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--prefix", default="GB-STABLE-R1")
    parser.add_argument("--case-file", type=Path)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    cases = built_in_cases(args.prefix)
    if args.case_file:
        cases = load_utf8_cases(args.case_file)
    if args.limit:
        cases = cases[: args.limit]

    output_dir = ROOT / ".runtime" / "stable-baseline"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"round-{args.round}-{args.prefix}.jsonl"
    ws_url = find_page()
    browser = Browser(ws_url)
    await browser.connect()
    try:
        await browser.open_panel()
        for case in cases:
            name = str(case.get("name") or "case")
            conversation_id = await browser.new_conversation(f"{args.prefix}-{name}")
            for index, turn in enumerate(case.get("turns") or [], start=1):
                text = str(turn.get("text") or "")
                if not text:
                    continue
                print(f"[{utc_now()}] {name} turn={index} {text[:80]}", flush=True)
                try:
                    result = await run_turn(browser, conversation_id, text, turn, args.timeout)
                except Exception as exc:  # keep independent cases running
                    result = {
                        "recorded_at": utc_now(),
                        "utterance": text,
                        "conversation_id": conversation_id,
                        "status": "HARNESS_ERROR",
                        "error": repr(exc),
                    }
                result.update({"round": args.round, "prefix": args.prefix, "case": name, "turn": index})
                with output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                print(f"  -> {result.get('status')} {result.get('elapsed_seconds', '')}", flush=True)
                if str(result.get("status") or "") in WAITING | {"HARNESS_ERROR", "FAILED"}:
                    print(f"  stopping case after unattended waiting/error: {result.get('status')}", flush=True)
                    break
    finally:
        await browser.close()
    print(json.dumps({"output": str(output), "round": args.round, "prefix": args.prefix}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
