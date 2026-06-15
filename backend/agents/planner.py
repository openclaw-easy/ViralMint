# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
Agent 1: Smart Chat/Planner
- Streams AI responses over WebSocket
- Parses <action> blocks and dispatches them
- Injects user context (behavior history + suggestions)
- Triggers setup wizards for missing config
- Handles direct URL downloads
- Proactively prompts for missing credentials
"""
import re
import json
import logging
from backend.core.ai_provider import get_ai_client
from backend.core.user_intelligence import UserIntelligence
from backend.core.setup_wizard import WIZARDS
from backend.core.ws_manager import ws_manager
from backend.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are ViralMint — the most capable AI content strategy assistant.
You are proactive, resourceful, and speak like a sharp colleague who always has the next move ready.
You have memory across sessions and know what the user was working on before.

## Your Full Capabilities

You can do ANYTHING related to video content strategy:

1. **Scout** — Find trending videos across ANY platform. YouTube, TikTok, Douyin have dedicated APIs (richer results). ALL other platforms (Bilibili, SoundCloud, Niconico, Instagram, Vimeo, etc.) are searched dynamically — no API key needed. You can put ANY platform name in the platforms list and it just works.
2. **Download ANY video from ANY platform** — 1000+ sites supported via yt-dlp. You NEVER refuse a download request. If a user gives you a URL, you download it — period.
3. **Transcribe & Analyze** — Extract transcripts, hooks, structure, tone, viral factors from any downloaded video
4. **Generate original videos** — Script → AI voice → AI visuals → captions → finished MP4
5. **Upload** — Publish to YouTube and TikTok on schedule
6. **Direct URL operations** — User pastes a URL? Download it. Analyze it. No questions asked. NEVER say "I don't support that platform". You support ALL platforms.

## CRITICAL Behavior Rules

### Be Proactive and Contextual — You Have Memory
- Reference the previous session naturally: "Welcome back! Last time you were looking at cooking videos — want to continue with that?"
- If there are recent failures, mention them: "Heads up — 2 downloads failed yesterday due to rate limiting. Want me to retry?"
- If a credential is expiring, warn early: "Your TikTok cookie is getting old (28 days) — want to refresh it before we scout?"
- Weave smart suggestions into conversation naturally. Don't list them like a menu — suggest the most relevant one as a natural next step.

### Examples of PROACTIVE behavior (this is what makes you feel smart):

User opens new session after scouting "morning routines" yesterday:
You: "Welcome back! Last time we found some great morning routine content — you downloaded 3 videos. Want me to generate a video from the best one, or scout a fresh batch?"

User says "hi" with 5 analyzed videos and 0 generated:
You: "Hey! You've got 5 analyzed competitor videos waiting. The one about '10-minute morning habits' had a virality score of 87 — want me to generate a video inspired by it?"
<action>{{"type": "show_downloaded"}}</action>

User asks to scout TikTok but cookie is 29 days old:
You: "I can scout TikTok, but heads up — your TikTok cookie is 29 days old and might stop working soon. Let me refresh it first, then we'll scout."
<action>{{"type": "start_wizard", "wizard_id": "tiktok_cookie"}}</action>

### ALWAYS Emit Action Blocks — This Is Non-Negotiable
- You are NOT a chatbot that just talks. You are an AGENT that DOES things.
- When the user asks you to do something, you MUST include an <action> block in your response. NEVER just say "I'll do that" without an action block.
- A response without an <action> block means NOTHING HAPPENS. The user will be left waiting.
- Keep your text brief (1-2 sentences) and put the <action> block at the end.

### Examples of CORRECT action behavior:
User: "analyze this channel https://youtube.com/@SomeChannel"
You: "Let me pull up an overview of that channel for you."
<action>{{"type": "analyze_channel", "url": "https://youtube.com/@SomeChannel"}}</action>

User: "scout personal finance videos"
You: "On it! Searching for trending personal finance content."
<action>{{"type": "start_scout", "niche": "personal finance", "platforms": ["youtube", "tiktok"]}}</action>

User: "找一下街边美食"
You: "马上搜索街边美食的热门内容！"
<action>{{"type": "start_scout", "niche": "街边美食", "platforms": ["youtube", "tiktok"]}}</action>

User: "download this video https://youtube.com/watch?v=abc123"
You: "Downloading and analyzing that video now."
<action>{{"type": "download_url", "url": "https://youtube.com/watch?v=abc123"}}</action>

### Examples of WRONG behavior (NEVER do this):
User: "找一下街边美食"
You: "正在为您搜索街边美食相关的热门内容。我会在 YouTube 和 TikTok 上查找。"
(NO ACTION BLOCK = NOTHING HAPPENS = USER WAITS FOREVER = THIS IS A BUG)

User: "analyze this channel https://youtube.com/@SomeChannel"
You: "I'll take a look at that channel and provide an overview. Give me a moment to gather the details."
(NO ACTION BLOCK = NOTHING HAPPENS = USER WAITS FOREVER = THIS IS A BUG)

### CRITICAL: Self-check before responding
Before sending your response, verify: "Did I include an <action> block?" If the user asked you to DO something (scout, download, analyze, generate, upload) and your response has no <action> block, your response is BROKEN. Add the action block.

### CRITICAL: Know When NOT to Act
- When the user says "thanks", "ok", "got it", "that's it", "no", "I'm done", "bye", or any other conversational acknowledgment — just respond conversationally. Do NOT trigger any action.
- NEVER re-trigger an action that was already completed in this conversation (e.g. don't re-analyze a channel that was just analyzed).
- Only emit an <action> block when the user is REQUESTING something new. Casual replies, follow-up questions about results, or acknowledgments are NOT requests.
- If unsure whether the user wants a new action, ASK — don't guess and trigger one.

### Handle URLs Intelligently
- If the user shares a **single video URL** from ANY platform, use `download_url` to download and analyze it immediately. We support 1000+ sites.
- If the user shares a **channel or playlist URL** (/@username, /channel/, /playlist?), use `analyze_channel` first to show a lightweight overview. Do NOT download all videos immediately.
- After showing channel analysis, suggest: "Want me to download the top 5 and do a deep analysis?"
- **NEVER say "I don't support this platform" or "I can't download from X".** We use yt-dlp which supports virtually every video platform. Just use `download_url` with whatever URL the user gives you.

### Be Proactive
- ALWAYS suggest the next logical step. Never leave the user hanging.
- After any action completes, immediately suggest what to do next.
- Push the pipeline forward: scout → download → analyze → generate → upload.

### Proactively Prompt for Missing Credentials
- Check the credential status above. If a key service is missing, proactively offer to set it up.
- For AI provider: "I notice you haven't set up an AI provider yet. Want me to walk you through it? It takes 2 minutes and unlocks everything."
- For YouTube API: "To scout YouTube trending videos, I need a YouTube API key. Want me to help you set one up? It's free."
- For voice/video generation: "You're ready to generate videos! Edge TTS is set up by default — want to try OpenAI TTS for premium quality? You'll need an OpenAI key."
- Use the start_wizard action to open the setup wizard — don't just tell them to go to Settings.
- Frame missing credentials as opportunities, not blockers: "You could also scout TikTok — want to set that up?"

### Be Concise but Actionable
- Use bullet points for options.
- Every response should end with a clear next action or question.
- Don't explain what you can do in abstract — just do it or offer to do it.
- Respond in the same language the user writes in.

### Suggest Expanding Scope
- After a scout: "Great results! Want me to also check TikTok/Douyin for the same niche?"
- After analysis: "I found 3 great angles. Want me to generate a video from the best one?"
- After generation: "Video is ready! Upload to YouTube now, or want to generate another variation?"
- Periodically: "Have you considered exploring [related niche]? It's trending right now."

## Available Actions

Output these JSON blocks at the END of your response to trigger actions:

```
<action>{{"type": "start_scout", "niche": "personal finance", "platforms": ["youtube", "tiktok", "douyin"]}}</action>
<action>{{"type": "analyze_channel", "url": "https://youtube.com/@ChannelName"}}</action>
<action>{{"type": "download_url", "url": "https://youtube.com/watch?v=xxx", "title": "optional title"}}</action>
<action>{{"type": "download_channel_videos", "url": "https://youtube.com/@ChannelName", "max_videos": 5}}</action>
<action>{{"type": "start_download", "scout_result_ids": ["id1", "id2"]}}</action>
<action>{{"type": "start_generate", "downloaded_video_id": "uuid"}}</action>
<action>{{"type": "start_upload", "generated_video_id": "uuid", "platforms": ["youtube", "tiktok"]}}</action>
<action>{{"type": "start_wizard", "wizard_id": "youtube_auth"}}</action>
<action>{{"type": "show_scout_results"}}</action>
<action>{{"type": "show_downloaded"}}</action>
<action>{{"type": "show_videos"}}</action>
<action>{{"type": "content_calendar", "days": 7}}</action>
```

### News Research Actions (NEW — intelligent news scouting)

You can research trending news and articles from the web:
- Search 12 sources: Google News, Bing News, Hacker News, Reddit, CNBC, BBC, Reuters, NY Times, The Guardian, Al Jazeera, TechCrunch, Yahoo News
- AI deeply analyzes each article: hook, video angle, talking points, key quotes
- User saves best articles to Library → generates video scripts from them
- Perfect for: news commentary, hot takes, explainers, weekly recaps, reaction content
- You do NOT need to specify sources — all 12 are searched by default

```
<action>{{"type": "start_news_scout", "query": "AI regulation", "expanded_queries": ["EU AI Act 2026", "OpenAI regulation news"]}}</action>
<action>{{"type": "analyze_url", "url": "https://cnbc.com/some-article"}}</action>
<action>{{"type": "save_news_to_library", "article_ids": ["id1", "id2"]}}</action>
```

IMPORTANT news intelligence rules:
- If the user says "scout trending news" or "find news" WITHOUT a specific topic, you MUST ask what topic they want BEFORE scouting. Do NOT pick a topic yourself. Just ask: "What topic should I research? For example: crypto, politics, AI regulation, climate change..."
- If the user's query is vague (just "trending", "news", "latest"), ask ONE clarifying question BEFORE scouting. Do NOT emit an action block.
- If the user pastes a direct article URL, analyze that single article (use `analyze_url`)
- If the input is gibberish, politely ask what topic they want
- Expand vague queries into 2-3 specific search terms in `expanded_queries`
- After showing results, proactively suggest: "Want me to save the top 3?" or "Generate a video from the best one?"

PROACTIVE news behavior:
- If user casually mentions a topic/niche: "Want me to find today's trending news about [topic]? Great for commentary videos."
- After video scouting: "I also found breaking news related to [niche] — want me to pull the top stories?"
- When user saves articles but doesn't generate: "You've got [N] articles saved — the [best one] has strong video potential."
- Be a content strategist, not a passive tool.

### When to Use Which Action

- `analyze_channel` — User shares a channel/playlist URL (/@, /channel/, /c/, /playlist) and wants to understand it. ALWAYS use this first for channel URLs. Never jump straight to downloading an entire channel.
- `download_url` — User shares a SINGLE video URL (youtube.com/watch?v=xxx) and wants to download/analyze it.
- `download_channel_videos` — User has ALREADY seen the channel analysis summary and explicitly asks to download videos from that channel. Only use AFTER analyze_channel.
- `start_scout` — User wants to search by niche/topic across platforms. You can use ANY platform name in the platforms list — the system handles it dynamically. Never refuse a platform.
- `start_download` — Download specific scout results by ID.
- `show_downloaded` — User asks about their downloaded/analyzed videos. Shows the list inline in chat with generate buttons.
- `show_videos` — User asks about generated videos. Navigates to videos page.
- `start_wizard` — Set up missing credentials when user agrees.
- `content_calendar` — User asks to "plan my content", "what should I post this week", etc. Generates an AI-powered day-by-day plan.

### CRITICAL: Think Before Acting
- When a user asks to "analyze" or "look at" a channel, FIRST present a lightweight overview. Do NOT download videos immediately.
- When a user shares a channel URL, use `analyze_channel` to show them what's there. Then ask what they want to do next.
- Only use `download_url` or `download_channel_videos` when the user explicitly wants to download specific videos.
- Be intelligent: gather lightweight info first → present summary → let user decide on heavy operations.

## Important: wizard_id values
Valid wizard IDs: youtube_auth, tiktok_upload_auth, telegram
Note: Scouting credentials (YouTube API key, TikHub token, Pexels) are configured via .env file or Settings page. If a platform's key is missing, that platform is skipped gracefully.

## ═══════ DYNAMIC CONTEXT (changes per request) ═══════

## User Profile (AI-generated from behavior patterns — use this to personalize)
{user_profile}

## Previous Session (what the user was doing last time — reference naturally if relevant)
{previous_session}

## Recent Failures (mention these proactively if relevant)
{recent_failures}

## User Context (current session stats)
{user_context}

## Live Pipeline State (where the user is in the funnel RIGHT NOW — be proactive about `next_best_action`)
{pipeline_state}

## Credential Status (what's configured vs missing)
{credential_status}

## Performance Insights (what content works best for this creator — use to guide recommendations)
{performance_insights}

## Smart Suggestions (offer these naturally, don't list them robotically)
{smart_suggestions}

## News Memory Context (user's news scouting history)
{news_context}
"""

ACTION_PATTERN = re.compile(r"<action>(.*?)</action>", re.DOTALL)


class PlannerAgent:
    def __init__(self):
        self.intelligence = UserIntelligence()

    async def handle_message(
        self,
        message: str,
        history: list[dict],
        user_settings,
        user_id: str = "local",
        previous_session_context: list[dict] = None,
    ):
        """
        Main handler: stream AI response over WS, then dispatch any <action> blocks.
        history: list of {"role": "user"|"assistant", "content": "..."}
        previous_session_context: last few messages from the user's previous session (for continuity)
        """
        logger.info("PLANNER handle_message | user=%s | msg=%s", user_id, message[:100])

        # Build context
        ctx = await self.intelligence.get_context_summary(user_id)
        suggestions = await self.intelligence.get_smart_suggestions(user_id)
        cred_status = await self.intelligence.get_credential_status(user_settings, user_id)
        user_profile = await self.intelligence.get_user_profile(user_id)
        recent_failures = await self.intelligence.get_recent_failures(user_id)

        # Format credential status with health warnings
        cred_lines = []
        cred_warnings = []
        for service, info in cred_status.items():
            status_str = "CONFIGURED" if info["configured"] else "NOT SET UP"
            wizard = info.get("setup_wizard", "")
            line = f"  {service}: {status_str}"
            if wizard:
                line += f" (wizard: {wizard})"
            # Add health warnings for cookies
            if info.get("age_days") is not None and info["age_days"] >= 25:
                days = info["age_days"]
                severity = "EXPIRED/CRITICAL" if days >= 30 else "EXPIRING SOON"
                line += f" ⚠️ {severity} ({days} days old)"
                cred_warnings.append(f"⚠️ {service} cookie is {days} days old — offer to refresh it via wizard")
            cred_lines.append(line)

        # Format user profile for the prompt
        profile_text = "No profile yet — this is a new or early user."
        if user_profile:
            profile_text = json.dumps(user_profile, indent=2, ensure_ascii=False)

        # Format previous session context
        prev_session_text = "None — this is the user's first session or a continuing session."
        if previous_session_context:
            prev_lines = []
            for m in previous_session_context:
                role = "User" if m["role"] == "user" else "You"
                prev_lines.append(f"  {role}: {m['content']}")
            prev_session_text = "\n".join(prev_lines)

        # Format recent failures
        failures_text = "None"
        if recent_failures:
            failures_text = "\n".join(f"  - {f}" for f in recent_failures)

        # Format performance insights
        perf_insights = await self.intelligence.get_performance_insights(user_id)
        perf_text = "No performance data yet — user hasn't uploaded enough videos."
        if perf_insights:
            perf_text = json.dumps(perf_insights, indent=2)

        # Build news memory context
        news_ctx = await self.intelligence.get_news_context(user_id)
        news_text = "No news scouting history yet."
        if news_ctx and news_ctx.get("total_news_scouts", 0) > 0:
            news_text = json.dumps(news_ctx, indent=2, ensure_ascii=False)

        # Live funnel state — where the user is right now (downloaded-not-generated,
        # generated-not-uploaded, etc.) + the single highest-value next step. This
        # is what makes the planner proactive instead of purely reactive.
        pipeline = await self.intelligence.get_pipeline_state(user_id)
        pipeline_text = json.dumps(pipeline, indent=2, ensure_ascii=False) if pipeline else "Unknown."

        system = SYSTEM_PROMPT.format(
            user_context=json.dumps(ctx, indent=2),
            pipeline_state=pipeline_text,
            smart_suggestions=json.dumps(suggestions),
            credential_status="\n".join(cred_lines),
            user_profile=profile_text,
            previous_session=prev_session_text,
            recent_failures=failures_text,
            performance_insights=perf_text,
            news_context=news_text,
        )

        # Build messages list
        messages = list(history)
        messages.append({"role": "user", "content": message})

        # Get AI client — BYOK from .env or per-user settings
        try:
            ai = get_ai_client(user_settings)
        except Exception:
            welcome = (
                "Welcome to ViralMint!\n\n"
                "To get started, set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in your `.env` file, "
                "or configure your provider and key in **Settings**."
            )
            await ws_manager.send({"type": "chat_token", "token": welcome}, user_id)
            await ws_manager.send({"type": "chat_done", "full_response": welcome}, user_id)
            return

        full_response = ""

        async for token in ai.chat_stream(messages=messages, system=system, max_tokens=1024):
            full_response += token
            await ws_manager.send({"type": "chat_token", "token": token}, user_id)

        # Signal completion
        clean_response = ACTION_PATTERN.sub("", full_response).strip()
        await ws_manager.send({"type": "chat_done", "full_response": clean_response}, user_id)

        # Parse and dispatch action blocks
        actions = ACTION_PATTERN.findall(full_response)
        logger.debug("PLANNER parsed %d action block(s) from AI response", len(actions))

        # Safety net: if AI talked about scouting but forgot the action block, auto-trigger
        if not actions:
            actions = self._infer_missing_action(message, full_response)
            if actions:
                logger.info("PLANNER safety-net inferred action: %s", actions)

        for action_json in actions:
            try:
                action = json.loads(action_json.strip())
                await self._dispatch_action(action, user_settings, user_id)
            except json.JSONDecodeError as e:
                logger.warning(f"Malformed action JSON, attempting AI repair: {action_json!r}")
                try:
                    from backend.core.ai_retry import ai_fix_action
                    repaired = await ai_fix_action(action_json, str(e), user_settings)
                    if repaired:
                        await self._dispatch_action(repaired, user_settings, user_id)
                    else:
                        logger.error(f"AI could not repair action JSON: {action_json!r}")
                except Exception as repair_err:
                    logger.error(f"Action repair failed: {repair_err}")

        # Record conversation event
        await self.intelligence.record_event("chat_message", {
            "user_message": message[:200],
            "actions_triggered": [json.loads(a) for a in actions if self._is_valid_json(a)],
        }, user_id)

        return clean_response

    async def handle_message_text(
        self,
        message: str,
        user_settings,
        user_id: str = "local",
    ) -> str:
        """
        Non-streaming sibling of handle_message() for messaging channels
        (Telegram, WhatsApp, Discord, Slack) that can't push WebSocket tokens.
        Builds the same system prompt and dispatches the same <action> blocks,
        but returns the full response string with action blocks stripped.
        """
        logger.info("PLANNER handle_message_text | user=%s | msg=%s", user_id, message[:100])

        # Same context-building as handle_message
        ctx = await self.intelligence.get_context_summary(user_id)
        suggestions = await self.intelligence.get_smart_suggestions(user_id)
        cred_status = await self.intelligence.get_credential_status(user_settings, user_id)
        user_profile = await self.intelligence.get_user_profile(user_id)
        recent_failures = await self.intelligence.get_recent_failures(user_id)

        cred_lines = []
        for service, info in cred_status.items():
            status_str = "CONFIGURED" if info["configured"] else "NOT SET UP"
            wizard = info.get("setup_wizard", "")
            line = f"  {service}: {status_str}"
            if wizard:
                line += f" (wizard: {wizard})"
            cred_lines.append(line)

        profile_text = (
            json.dumps(user_profile, indent=2, ensure_ascii=False)
            if user_profile else "No profile yet."
        )
        failures_text = "\n".join(f"  - {f}" for f in recent_failures) if recent_failures else "None"

        perf_insights = await self.intelligence.get_performance_insights(user_id)
        perf_text = json.dumps(perf_insights, indent=2) if perf_insights else "No performance data yet."

        news_ctx = await self.intelligence.get_news_context(user_id)
        news_text = (
            json.dumps(news_ctx, indent=2, ensure_ascii=False)
            if news_ctx and news_ctx.get("total_news_scouts", 0) > 0
            else "No news scouting history yet."
        )

        # Live funnel state — cheap (indexed counts) and high-value even for
        # messaging: it's how the bot proactively nudges the next step.
        pipeline = await self.intelligence.get_pipeline_state(user_id)
        pipeline_text = json.dumps(pipeline, ensure_ascii=False) if pipeline else "Unknown."

        system = SYSTEM_PROMPT.format(
            user_context=json.dumps(ctx, indent=2),
            pipeline_state=pipeline_text,
            smart_suggestions=json.dumps(suggestions),
            credential_status="\n".join(cred_lines),
            user_profile=profile_text,
            previous_session="N/A — inbound from messaging channel.",
            recent_failures=failures_text,
            performance_insights=perf_text,
            news_context=news_text,
        )

        try:
            ai = get_ai_client(user_settings)
        except Exception:
            return (
                "AI provider is not configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY "
                "in your .env, or configure your key in Settings."
            )

        try:
            full_response = await ai.chat(
                messages=[{"role": "user", "content": message}],
                system=system,
                max_tokens=1024,
            )
        except Exception as e:
            logger.exception("PLANNER text chat failed: %s", e)
            return "Sorry — I couldn't reach the AI backend just now. Try again in a moment."

        clean_response = ACTION_PATTERN.sub("", full_response).strip()
        actions = ACTION_PATTERN.findall(full_response)
        if not actions:
            actions = self._infer_missing_action(message, full_response)

        for action_json in actions:
            try:
                action = json.loads(action_json.strip())
                await self._dispatch_action(action, user_settings, user_id)
            except json.JSONDecodeError:
                logger.warning("Malformed action JSON in text path: %r", action_json)
            except Exception as e:
                logger.exception("Action dispatch failed in text path: %s", e)

        await self.intelligence.record_event(
            "chat_message",
            {
                "user_message": message[:200],
                "source": "messaging",
                "actions_triggered": [json.loads(a) for a in actions if self._is_valid_json(a)],
            },
            user_id,
        )

        return clean_response or "Working on it. ✅"

    async def _dispatch_action(self, action: dict, user_settings, user_id: str):
        action_type = action.get("type")
        logger.info("DISPATCH action=%s | user=%s | payload=%s", action_type, user_id, json.dumps(action, ensure_ascii=False)[:200])

        if action_type == "start_scout":
            await self._check_and_start_scout(action, user_settings, user_id)

        elif action_type == "analyze_channel":
            await self._analyze_channel(action, user_id)

        elif action_type == "download_channel_videos":
            await self._download_channel_videos(action, user_id)

        elif action_type == "download_url":
            await self._download_url(action, user_settings, user_id)

        elif action_type == "start_wizard":
            wizard_id = action.get("wizard_id")
            if wizard_id in WIZARDS:
                await ws_manager.send({
                    "type": "wizard_start",
                    "wizard_id": wizard_id,
                    "wizard": WIZARDS[wizard_id],
                }, user_id)

        elif action_type == "start_download":
            scout_result_ids = action.get("scout_result_ids", [])
            if scout_result_ids:
                from backend.agents.job_helper import create_job
                from backend.core.task_runner import run_download, dispatch
                job = await create_job("download", user_id, {"scout_result_ids": scout_result_ids})
                dispatch(run_download(job_id=job.id, scout_result_ids=scout_result_ids, user_id=user_id))
                await ws_manager.send({
                    "type": "job_started",
                    "job_id": job.id,
                    "job_type": "download",
                    "message": f"Downloading {len(scout_result_ids)} videos...",
                }, user_id)

        elif action_type == "start_generate":
            downloaded_video_id = action.get("downloaded_video_id")
            if downloaded_video_id:
                from backend.agents.job_helper import create_job
                from backend.core.task_runner import run_generate, dispatch
                job = await create_job("generate", user_id, {"downloaded_video_id": downloaded_video_id})
                dispatch(run_generate(
                    job_id=job.id, downloaded_video_id=downloaded_video_id, user_id=user_id,
                ))

        elif action_type == "start_upload":
            generated_video_id = action.get("generated_video_id")
            platforms = action.get("platforms", ["youtube"])
            if generated_video_id:
                from backend.agents.job_helper import create_job
                from backend.core.task_runner import run_upload, dispatch
                job = await create_job("upload", user_id, {"generated_video_id": generated_video_id})
                dispatch(run_upload(
                    job_id=job.id, generated_video_id=generated_video_id, platforms=platforms, user_id=user_id,
                ))

        elif action_type == "show_scout_results":
            await self._show_scout_results(action, user_id)

        elif action_type == "show_downloaded":
            await self._show_downloaded(user_id)

        elif action_type == "show_videos":
            await self._show_videos(user_id)

        elif action_type == "content_calendar":
            days = action.get("days", 7)
            calendar = await self.intelligence.generate_content_calendar(user_id, days)
            if calendar:
                await ws_manager.send({
                    "type": "content_calendar",
                    "calendar": calendar,
                }, user_id)
            else:
                await ws_manager.send({
                    "type": "chat_token",
                    "token": "\n\nI need more data to generate a personalized content calendar. Try uploading a few videos first so I can learn what works for your audience.",
                }, user_id)

        elif action_type == "start_news_scout":
            await self._start_news_scout(action, user_id)

        elif action_type == "analyze_url":
            await self._analyze_article_url(action, user_id)

        elif action_type == "save_news_to_library":
            await self._save_news_to_library(action, user_id)

    async def _start_news_scout(self, action: dict, user_id: str):
        """Start a news scout job."""
        query = action.get("query", "")
        if not query:
            return
        expanded_queries = action.get("expanded_queries", [])
        sources = action.get("sources")  # None = all 12 sources (default in scraper)

        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_news_scout, dispatch

        job = await create_job("news_scout", user_id, {
            "query": query,
            "expanded_queries": expanded_queries,
            "sources": sources,
        })
        dispatch(run_news_scout(
            job_id=job.id, query=query,
            expanded_queries=expanded_queries or None,
            sources=sources or None,
            user_id=user_id,
        ))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "news_scout",
            "message": f"Researching '{query}' across {', '.join(sources) if sources else 'all 12 news sources'}...",
        }, user_id)
        await self.intelligence.record_event("news_scouted", {
            "query": query,
            "expanded_queries": expanded_queries,
            "sources": sources,
        }, user_id)

    async def _analyze_article_url(self, action: dict, user_id: str):
        """Analyze a single article URL."""
        url = action.get("url", "").strip()
        if not url:
            return

        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_news_scout, dispatch

        job = await create_job("news_scout", user_id, {"direct_url": url})
        dispatch(run_news_scout(
            job_id=job.id, query="direct URL analysis",
            direct_url=url, user_id=user_id,
        ))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "news_scout",
            "message": "Analyzing article...",
        }, user_id)

    async def _save_news_to_library(self, action: dict, user_id: str):
        """Save selected news articles to Library."""
        article_ids = action.get("article_ids", [])
        if not article_ids:
            return

        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_news_save, dispatch

        job = await create_job("news_save", user_id, {"article_ids": article_ids})
        dispatch(run_news_save(job_id=job.id, article_ids=article_ids, user_id=user_id))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "news_save",
            "message": f"Saving {len(article_ids)} article{'s' if len(article_ids) != 1 else ''} to Library...",
        }, user_id)
        await self.intelligence.record_event("news_saved", {
            "article_ids": article_ids,
            "count": len(article_ids),
        }, user_id)

    async def _show_scout_results(self, action: dict, user_id: str):
        """Fetch recent scout results from DB and send them over WS."""
        from backend.database import AsyncSessionLocal
        from backend.models.scout_result import ScoutResult
        from sqlalchemy import select

        job_id = action.get("job_id")
        limit = action.get("limit", 50)

        async with AsyncSessionLocal() as db:
            query = (
                select(ScoutResult)
                .where(ScoutResult.user_id == user_id)
                .order_by(ScoutResult.created_at.desc())
                .limit(limit)
            )
            if job_id:
                query = query.where(ScoutResult.job_id == job_id)
            result = await db.execute(query)
            results = result.scalars().all()

        if not results:
            await ws_manager.send({
                "type": "chat_token",
                "token": "\n\nNo scout results found yet. Try scouting a niche first!",
            }, user_id)
            return

        # Group by platform and send
        platforms = {}
        for r in results:
            platforms.setdefault(r.platform, []).append({
                "id": r.id,
                "platform": r.platform,
                "video_id": r.video_id,
                "title": r.title,
                "author": r.author,
                "author_url": r.author_url,
                "views": r.views,
                "likes": r.likes,
                "comments": r.comments,
                "duration_seconds": r.duration_seconds,
                "upload_date": r.upload_date.isoformat() if r.upload_date else None,
                "virality_score": r.virality_score,
                "thumbnail_url": r.thumbnail_url,
                "video_url": r.video_url,
                "embed_url": r.embed_url,
            })

        for platform, items in platforms.items():
            await ws_manager.send({
                "type": "scout_results",
                "job_id": job_id or "",
                "platform": platform,
                "total": len(items),
                "results": items,
            }, user_id)

    async def _show_downloaded(self, user_id: str):
        """Fetch downloaded videos from DB and send them as a rich list in chat."""
        from backend.database import AsyncSessionLocal
        from backend.models.downloaded_video import DownloadedVideo
        from backend.models.scout_result import ScoutResult
        from sqlalchemy import select, outerjoin
        import json as _json

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DownloadedVideo)
                .where(DownloadedVideo.user_id == user_id)
                .order_by(DownloadedVideo.created_at.desc())
                .limit(20)
            )
            downloads = result.scalars().all()

            # Fetch associated scout results for titles/thumbnails
            scout_ids = [d.scout_result_id for d in downloads if d.scout_result_id]
            scouts_map = {}
            if scout_ids:
                sr = await db.execute(
                    select(ScoutResult).where(ScoutResult.id.in_(scout_ids))
                )
                for s in sr.scalars().all():
                    scouts_map[s.id] = s

        if not downloads:
            await ws_manager.send({
                "type": "chat_token",
                "token": "\n\nNo downloaded videos yet. Try scouting a niche and downloading some videos first!",
            }, user_id)
            return

        items = []
        for d in downloads:
            scout = scouts_map.get(d.scout_result_id)
            insights = _json.loads(d.insights_json) if d.insights_json else {}
            items.append({
                "id": d.id,
                "title": (scout.title if scout else None) or d.video_path or "Untitled",
                "thumbnail_url": scout.thumbnail_url if scout else None,
                "platform": scout.platform if scout else None,
                "views": scout.views if scout else None,
                "duration_seconds": d.duration_seconds,
                "transcript_preview": (d.transcript or "")[:150],
                "has_insights": bool(d.insights_json),
                "suggested_angle": insights.get("suggested_angle", ""),
                "created_at": d.created_at.isoformat() if d.created_at else None,
            })

        await ws_manager.send({
            "type": "downloaded_list",
            "total": len(items),
            "videos": items,
        }, user_id)

    async def _show_videos(self, user_id: str):
        """Send a nudge to navigate to videos page."""
        await ws_manager.send({
            "type": "action",
            "action": {"type": "navigate", "path": "/videos"},
        }, user_id)

    async def _analyze_channel(self, action: dict, user_id: str):
        """Lightweight channel analysis — fetch metadata + video list without downloading."""
        url = action.get("url", "").strip()
        if not url:
            return

        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_analyze_channel, dispatch

        job = await create_job("analyze", user_id, {"url": url, "type": "channel_analysis"})
        dispatch(run_analyze_channel(job_id=job.id, url=url, user_id=user_id))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "analyze",
            "message": f"Analyzing channel...",
        }, user_id)

    async def _download_channel_videos(self, action: dict, user_id: str):
        """Download top N videos from a channel after user has seen the analysis."""
        url = action.get("url", "").strip()
        max_videos = action.get("max_videos", 5)
        if not url:
            return

        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_download_url, dispatch

        job = await create_job("download", user_id, {"url": url, "channel_download": True, "max_videos": max_videos})
        dispatch(run_download_url(job_id=job.id, url=url, title="", user_id=user_id))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "download",
            "message": f"Downloading top {max_videos} videos from channel...",
        }, user_id)

    async def _download_url(self, action: dict, _user_settings, user_id: str):
        """Download a video directly from a URL provided by the user."""
        url = action.get("url", "").strip()
        title = action.get("title", "")

        if not url:
            return

        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_download_url, dispatch

        job = await create_job("download", user_id, {
            "url": url,
            "title": title,
            "direct_url": True,
        })
        dispatch(run_download_url(job_id=job.id, url=url, title=title, user_id=user_id))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "download",
            "message": f"Downloading video from URL...",
        }, user_id)

    # Platforms that need API credentials — keys come from .env (BYOK).
    _CREDENTIAL_PLATFORMS = {
        "youtube": {
            "check": lambda us: bool(settings.YOUTUBE_API_KEY),
        },
        "tiktok": {
            "check": lambda us: (
                bool(settings.TIKHUB_API_KEY)
                or (us and us.tiktok_cookie_encrypted)
            ),
        },
        "douyin": {
            "check": lambda us: (
                bool(settings.TIKHUB_API_KEY)
                or (us and us.douyin_cookie_encrypted)
            ),
        },
    }

    async def _check_and_start_scout(self, action: dict, user_settings, user_id: str):
        """Start a scout job. Unavailable platforms are skipped gracefully."""
        niche = action.get("niche", "")
        platforms = action.get("platforms", ["youtube"])

        ready_platforms = []
        skipped = []

        for platform in platforms:
            cred_info = self._CREDENTIAL_PLATFORMS.get(platform)
            if cred_info:
                if cred_info["check"](user_settings):
                    ready_platforms.append(platform)
                else:
                    skipped.append(platform)
            else:
                ready_platforms.append(platform)

        if not ready_platforms:
            await ws_manager.send({
                "type": "chat_token",
                "token": "\n\nNo platforms available for scouting — please configure API keys in Settings.",
            }, user_id)
            return

        if skipped:
            await ws_manager.send({
                "type": "chat_token",
                "token": f"\n\nNote: {', '.join(skipped)} unavailable (no API key configured) — "
                         f"scouting on {', '.join(ready_platforms)} only.",
            }, user_id)

        # Kick off scout
        from backend.agents.job_helper import create_job
        from backend.core.task_runner import run_scout, dispatch
        job = await create_job("scout", user_id, {"niche": niche, "platforms": ready_platforms})
        dispatch(run_scout(job_id=job.id, niche=niche, platforms=ready_platforms, user_id=user_id))
        await ws_manager.send({
            "type": "job_started",
            "job_id": job.id,
            "job_type": "scout",
            "message": f"Scouting '{niche}' on {', '.join(ready_platforms)}...",
        }, user_id)
        await self.intelligence.record_event("niche_searched", {"niche": niche, "platforms": ready_platforms}, user_id)

    @staticmethod
    def _infer_missing_action(user_message: str, ai_response: str) -> list[str]:
        """
        Safety net: if the AI clearly intended to scout/search but forgot the <action> block,
        infer the action from the user message. Returns list of action JSON strings.
        """
        msg_lower = user_message.lower()
        resp_lower = ai_response.lower()

        # Detect direct article URL — always trigger analyze_url
        import re as _re
        url_match = _re.search(r'(https?://[^\s<>"\']+)', user_message)
        if url_match:
            url = url_match.group(1).rstrip(".,;:)")
            # Check if it's a news/article URL (not a YouTube/TikTok/Douyin video)
            video_domains = ["youtube.com", "youtu.be", "tiktok.com", "douyin.com"]
            is_video_url = any(d in url.lower() for d in video_domains)
            if not is_video_url:
                return [json.dumps({"type": "analyze_url", "url": url})]

        # Detect news scout intent
        news_keywords = ["news", "article", "headlines", "新闻", "热点", "资讯"]
        has_news_intent = any(kw in msg_lower for kw in news_keywords)

        # Detect scout intent from user message
        scout_keywords = [
            "scout", "search", "find", "look for", "trending",
            "找", "搜索", "搜一下", "查找", "热门", "帮我找",
        ]
        has_scout_intent = any(kw in msg_lower for kw in scout_keywords)

        # Detect that AI claimed it was doing something
        doing_keywords = [
            "searching", "scouting", "looking", "on it", "i'll search", "let me find",
            "正在搜索", "正在为您", "开始搜索", "马上", "开始为您",
        ]
        ai_claimed_action = any(kw in resp_lower for kw in doing_keywords)

        if has_scout_intent and ai_claimed_action:
            # Extract niche from user message — strip common prefixes
            niche = user_message.strip()
            for prefix in ["scout ", "search ", "find ", "look for ", "找一下", "找", "搜索", "搜一下", "帮我找", "查找"]:
                if niche.lower().startswith(prefix):
                    niche = niche[len(prefix):].strip()
                    break

            if niche:
                # If the user mentioned "news"/"article", use news scout
                if has_news_intent:
                    # Strip "news" from the query for cleaner search
                    clean_query = niche
                    for word in ["news", "articles", "headlines", "新闻", "热点", "资讯"]:
                        clean_query = clean_query.replace(word, "").strip()
                    clean_query = clean_query or niche
                    # Don't auto-trigger for vague queries like "trending news" — let AI ask
                    vague_queries = {"trending", "trending news", "latest", "latest news", "news", "headlines"}
                    if clean_query.lower() in vague_queries:
                        return []  # Let the AI ask what topic
                    action = json.dumps({
                        "type": "start_news_scout",
                        "query": clean_query,
                    })
                    return [action]

                action = json.dumps({
                    "type": "start_scout",
                    "niche": niche,
                    "platforms": ["youtube", "tiktok"],
                })
                return [action]

        return []

    @staticmethod
    def _is_valid_json(s: str) -> bool:
        try:
            json.loads(s)
            return True
        except Exception:
            return False
