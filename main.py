"""Learn how group members talk and let the bot borrow their expressions.

Group members' text messages are collected per group. Every so often a
model sums up the distinctive ones as "when <situation>, <style>"; an
embedding model merges paraphrases so the count shows what is really common.
Before each chat reply a few expressions that fit the current chat are
appended to the request. A WebUI page lists, edits and deletes them.
"""

import asyncio
import json
import re
import time

import numpy as np
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart

from .store import ExpressionStore, unit_vector

PLUGIN_NAME = "astrbot_plugin_expression_learner"
# Longer messages are mostly pasted text or explanations, not a way of talking.
MAX_MESSAGE_CHARS = 100
LINK_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
# Messages before the batch that are shown to the model for context only.
CONTEXT_MESSAGES = 10
# A backlog is learned in steps of at most this many messages.
MAX_BATCH = 120
MAX_PER_BATCH = 5
MAX_EXPRESSION_CHARS = 40
# Merge threshold for "situation|style" text similarity when no embedding
# model is configured.
TEXT_MERGE_THRESHOLD = 0.8
QUERY_MESSAGES = 4
EMBED_TIMEOUT = 8
# After an embedding failure, use text matching for this long before retrying.
EMBED_RETRY_AFTER = 300
REBUILD_BATCH = 32

LEARN_PROMPT = """下面是一个 QQ 群的聊天记录，每行开头的 [编号] 是消息编号，冒号前是发言人的昵称。
编号小于 {first} 的消息只是前文，帮你理解语境；只从编号 {first} 到 {last} 的消息里学。

任务：找出群友有特色、值得模仿的说话方式，总结成「当某种情境时，用某种表达」。

要求：
1. situation 写情境：概括这句话在什么场景下说，不超过 20 字。不要出现具体的人名、群名、作品名、商品名这类专有名词。
2. style 写表达：具体的说法或句式，不超过 20 字。会变的部分用 XX 代替，比如「用『已严肃使用XX』」「句尾加『捏』」「单独发一个『？』」。
3. 只收有特色的。「好的」「哈哈」「是的」「我也是」这种谁都会说的不要；群友对机器人提的要求也不要。
4. 梗和固定说法也算，但要能离开这段聊天单独用。
5. 宁缺毋滥：没有值得学的就输出 []，最多 {max_items} 条。
6. id 写这个说法出自的那条消息的编号。

只输出一个 JSON 数组，不要别的文字，例如：
[{{"situation": "对价格感到吃惊", "style": "直接说『这么贵』", "id": 12}}]

聊天记录：
{lines}"""

INJECT_HEADER = (
    "下面是从群友聊天里学到的说法，觉得合适就自然地借用，不用每条都用，也不要生搬硬套："
)


def embed_text(situation: str, style: str) -> str:
    """Text that represents one expression for the embedding model."""
    return f"当{situation}时，{style}"


def parse_expressions(text: str, first: int, last: int) -> list[tuple[str, str, int]]:
    """Read the model's JSON answer.

    Args:
        text: The model's reply.
        first: Lowest message number that may be a source.
        last: Highest message number that may be a source.

    Returns:
        (situation, style, source message number) for every usable entry.
    """
    match = re.search(r"\[\s*(?:\{|\])", text)
    end = text.rfind("]")
    if not match or end < match.start():
        return []
    try:
        data = json.loads(text[match.start() : end + 1])
    except ValueError:
        return []
    items: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        situation = str(entry.get("situation") or "").strip()
        style = str(entry.get("style") or "").strip()
        try:
            source = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if (
            not situation
            or not style
            or len(situation) > MAX_EXPRESSION_CHARS
            or len(style) > MAX_EXPRESSION_CHARS
            or not first <= source <= last
            or (situation, style) in seen
        ):
            continue
        seen.add((situation, style))
        items.append((situation, style, source))
    return items[:MAX_PER_BATCH]


@register(
    PLUGIN_NAME,
    "happycola233",
    "学习群友的说话方式（情境 → 表达），回复时挑几条合适的让机器人借用",
    "1.0.0",
)
class ExpressionLearner(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = ExpressionStore(
            StarTools.get_data_dir(PLUGIN_NAME) / "expressions.db"
        )
        self.locks: dict[str, asyncio.Lock] = {}
        # Latest session of each group, so learning uses that group's model.
        self.umos: dict[str, str] = {}
        # group_id -> "done/total" while vectors are being rebuilt.
        self.rebuilding: dict[str, str] = {}
        self.embed_failed_at = 0.0
        self.tasks: set[asyncio.Task] = set()
        for path, handler, methods, description in (
            ("overview", self.api_overview, ["GET"], "表达学习概况"),
            ("list", self.api_list, ["GET"], "表达列表"),
            ("update", self.api_update, ["POST"], "编辑表达"),
            ("delete", self.api_delete, ["POST"], "删除表达"),
            ("learn", self.api_learn, ["POST"], "立即学习"),
            ("rebuild", self.api_rebuild, ["POST"], "重建向量"),
            ("clear", self.api_clear, ["POST"], "清空一个群的表达"),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/{path}", handler, methods, description
            )

    async def terminate(self):
        for task in self.tasks:
            task.cancel()
        self.store.close()

    # ---- config ----

    def _ids(self, key: str) -> list[str]:
        return [
            str(value).strip()
            for value in self.config.get(key) or []
            if str(value).strip()
        ]

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _lock(self, group_id: str) -> asyncio.Lock:
        return self.locks.setdefault(group_id, asyncio.Lock())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ---- embedding ----

    def _embedder(self) -> tuple[object | None, str]:
        """Return (embedding provider or None, configured provider id)."""
        provider_id = str(self.config.get("embedding_provider_id") or "").strip()
        if not provider_id:
            return None, ""
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None or not hasattr(provider, "get_embeddings"):
            return None, provider_id
        return provider, provider_id

    async def _embed(
        self, texts: list[str], retry: bool = False
    ) -> list[np.ndarray] | None:
        """Embed texts as unit vectors, or return None to fall back to text.

        Args:
            texts: Texts to embed.
            retry: Ignore the pause after a recent failure.

        Returns:
            One vector per text, or None when no model is usable.
        """
        provider, _ = self._embedder()
        if provider is None or not texts:
            return None
        if not retry and time.time() - self.embed_failed_at < EMBED_RETRY_AFTER:
            return None
        try:
            vectors = await asyncio.wait_for(
                provider.get_embeddings(texts), EMBED_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001
            self.embed_failed_at = time.time()
            logger.warning(
                f"Embedding request failed, using text matching for now: {e}"
            )
            return None
        if len(vectors) != len(texts):
            return None
        return [unit_vector(vector) for vector in vectors]

    # ---- learning ----

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=50)
    async def collect(self, event: AstrMessageEvent):
        """Keep group members' plain text for the next learning batch.

        Runs after QQ 消息增强 (priority 100), so voice transcripts carry their
        "[语音]" tag and merged forwards are expanded; both are skipped, as are
        the bot's own messages, blacklisted senders, messages that @ the bot,
        commands, links and long texts.

        Args:
            event: The incoming group message.
        """
        group_id = str(event.get_group_id() or "")
        if group_id not in self._ids("learn_groups"):
            return
        self_id = str(event.get_self_id())
        sender = str(event.get_sender_id())
        if sender == self_id or sender in self._ids("blacklist"):
            return
        chain = event.get_messages()
        if any(isinstance(comp, At) and str(comp.qq) == self_id for comp in chain):
            return
        text = "".join(comp.text for comp in chain if isinstance(comp, Plain)).strip()
        if (
            not text
            or len(text) > MAX_MESSAGE_CHARS
            or text.startswith(("/", "[语音]"))
            or "[合并转发]" in text
            or LINK_PATTERN.search(text)
        ):
            return
        self.umos[group_id] = event.unified_msg_origin
        self.store.add_message(group_id, event.get_sender_name() or sender, text)
        if (
            not self._lock(group_id).locked()
            and self.store.unlearned_count(group_id)
            >= max(1, self._int("batch_size", 40))
            and time.time() - float(self.store.get_state(f"last_learn:{group_id}", "0"))
            >= self._int("min_interval_minutes", 10) * 60
        ):
            self._spawn(self.learn(group_id))

    def _learn_provider_id(self, group_id: str) -> str:
        provider_id = str(self.config.get("learn_provider_id") or "").strip()
        if provider_id:
            return provider_id
        provider = self.context.get_using_provider(self.umos.get(group_id))
        return provider.meta().id if provider else ""

    async def learn(self, group_id: str) -> dict:
        """Learn from the group's messages that were not learned yet.

        Args:
            group_id: Group to learn.

        Returns:
            Counts of messages read and expressions added, merged and
            removed by the cap, or {"error": message}.
        """
        async with self._lock(group_id):
            self.store.set_state(f"last_learn:{group_id}", str(time.time()))
            context_rows, new_rows = self.store.learning_batch(
                group_id, MAX_BATCH, CONTEXT_MESSAGES
            )
            if not new_rows:
                return {"messages": 0, "added": 0, "merged": 0, "removed": 0}
            rows = context_rows + new_rows
            first = len(context_rows) + 1
            prompt = LEARN_PROMPT.format(
                first=first,
                last=len(rows),
                max_items=MAX_PER_BATCH,
                lines="\n".join(
                    f"[{number}] {row['sender']}: {row['text']}"
                    for number, row in enumerate(rows, start=1)
                ),
            )
            provider_id = self._learn_provider_id(group_id)
            if not provider_id:
                return {"error": "没有可用的对话模型"}
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=prompt
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Expression learning failed in group {group_id}: {e}")
                return {"error": f"学习模型调用失败（{e}）"}

            items = parse_expressions(response.completion_text or "", first, len(rows))
            vectors = (
                await self._embed([embed_text(s, t) for s, t, _ in items])
                if items
                else None
            )
            _, model = self._embedder()
            threshold = float(self.config.get("merge_threshold", 0.9))
            added = merged = 0
            for index, (situation, style, source) in enumerate(items):
                vector = vectors[index] if vectors else None
                result = self.store.add_or_merge(
                    group_id,
                    situation,
                    style,
                    rows[source - 1]["text"],
                    vector,
                    model,
                    threshold if vector is not None else TEXT_MERGE_THRESHOLD,
                )
                if result == "added":
                    added += 1
                else:
                    merged += 1
            self.store.mark_learned(group_id, new_rows[-1]["id"])
            removed = self.store.enforce_cap(group_id, self._int("max_per_group", 5000))
            logger.info(
                f"Expression learning in group {group_id}: {len(new_rows)} messages, "
                f"{added} added, {merged} merged, {removed} removed by the cap"
            )
            return {
                "messages": len(new_rows),
                "added": added,
                "merged": merged,
                "removed": removed,
            }

    # ---- using ----

    @filter.on_llm_request(priority=-100)
    async def add_expressions(self, event: AstrMessageEvent, req: ProviderRequest):
        """Append a few fitting expressions to the end of the user turn.

        Runs after AngelHeart (50) and angel_memory (40). The part is marked
        temporary, so it is not saved into the conversation history, and it
        is at the end of the request, so the cached system prompt is unchanged.

        Args:
            event: The message being answered.
            req: The chat request about to be sent.
        """
        count = self._int("pick_count", 5)
        groups = self._ids("learn_groups")
        group_id = str(event.get_group_id() or "")
        if count <= 0:
            return
        if group_id:
            if group_id not in groups:
                return
            scope = [group_id]
            query = "\n".join(self.store.recent_texts(group_id, QUERY_MESSAGES))
        elif self.config.get("use_in_private", True):
            scope, query = groups, ""
        else:
            return
        candidates = self.store.candidates(
            scope, bool(self.config.get("featured_only", False))
        )
        if not candidates:
            return
        query = f"{query}\n{event.message_str or ''}".strip()
        similarity = None
        if query:
            vectors = await self._embed([query])
            if vectors:
                _, model = self._embedder()
                similarity = {}
                for scope_group in scope:
                    similarity.update(
                        self.store.similarities(scope_group, model, vectors[0])
                    )
        chosen = self.store.pick(candidates, similarity, count)
        if not chosen:
            return
        lines = "\n".join(f"- 当{row['situation']}时：{row['style']}" for row in chosen)
        req.extra_user_content_parts.append(
            TextPart(
                text=f"<群里的说话方式>\n{INJECT_HEADER}\n{lines}\n</群里的说话方式>"
            ).mark_as_temp()
        )
        self.store.mark_used([row["id"] for row in chosen])

    # ---- commands ----

    @filter.command_group("表达")
    def expression(self):
        """表达学习的管理指令。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @expression.command("学")
    async def learn_command(self, event: AstrMessageEvent):
        """马上学一次：在群里只学这个群，私聊里学所有配置的群。"""
        event.stop_event()
        group_id = str(event.get_group_id() or "")
        groups = [group_id] if group_id else self._ids("learn_groups")
        if group_id and group_id not in self._ids("learn_groups"):
            await event.send(MessageChain().message("这个群不在「学习的群」里。"))
            return
        if not groups:
            await event.send(MessageChain().message("还没有配置「学习的群」。"))
            return
        lines = []
        for target in groups:
            result = await self.learn(target)
            if "error" in result:
                lines.append(f"{target}：{result['error']}")
            else:
                lines.append(
                    f"{target}：读了 {result['messages']} 条消息，新学 {result['added']} 条，"
                    f"合并 {result['merged']} 条"
                )
        await event.send(MessageChain().message("\n".join(lines)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @expression.command("状态")
    async def status_command(self, event: AstrMessageEvent):
        """看每个群学了多少、还有多少消息没学。"""
        event.stop_event()
        provider, model = self._embedder()
        lines = []
        for group_id in self._ids("learn_groups"):
            stats = self.store.stats(group_id, model if provider else "")
            lines.append(
                f"{group_id}：{stats['total']} 条（精选 {stats['featured']}，"
                f"近 7 天新学 {stats['new_7d']}），待学消息 {stats['unlearned']} 条"
            )
        if not lines:
            lines.append("还没有配置「学习的群」。")
        if model:
            lines.append(
                f"嵌入模型：{model}"
                + ("" if provider else "（找不到这个模型，先按文字匹配）")
            )
        else:
            lines.append("嵌入模型：没配，按文字匹配")
        await event.send(MessageChain().message("\n".join(lines)))

    # ---- management page ----

    async def api_overview(self):
        provider, model = self._embedder()
        learn_groups = self._ids("learn_groups")
        groups = []
        for group_id in sorted(set(learn_groups) | set(self.store.groups())):
            groups.append(
                {
                    "group_id": group_id,
                    "learning_enabled": group_id in learn_groups,
                    "learning": self._lock(group_id).locked(),
                    "last_learn": float(
                        self.store.get_state(f"last_learn:{group_id}", "0")
                    ),
                    "rebuilding": self.rebuilding.get(group_id, ""),
                    **self.store.stats(group_id, model if provider else ""),
                }
            )
        return json_response(
            {
                "groups": groups,
                "embedding": {
                    "configured": model,
                    "active": provider is not None,
                    "available": [
                        p.meta().id for p in self.context.get_all_embedding_providers()
                    ],
                },
                "settings": {
                    "pick_count": self._int("pick_count", 5),
                    "featured_only": bool(self.config.get("featured_only", False)),
                    "batch_size": self._int("batch_size", 40),
                    "use_in_private": bool(self.config.get("use_in_private", True)),
                },
            }
        )

    async def api_list(self):
        group_id = str(request.query.get("group", "") or "")
        page = max(1, request.query.get("page", 1, type=int) or 1)
        size = min(max(request.query.get("size", 30, type=int) or 30, 10), 100)
        items, total = self.store.search(
            group_id,
            str(request.query.get("q", "") or "").strip(),
            str(request.query.get("sort", "learned") or "learned"),
            request.query.get("featured", "0") == "1",
            page,
            size,
        )
        return json_response(
            {"items": items, "total": total, "page": page, "size": size}
        )

    async def api_update(self):
        payload = await request.json(default={})
        try:
            expression_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少 id")
        situation = payload.get("situation")
        style = payload.get("style")
        if (situation is None) != (style is None):
            return error_response("情境和表达要一起改")
        if situation is not None:
            situation, style = str(situation).strip(), str(style).strip()
            if not situation or not style:
                return error_response("情境和表达都不能为空")
            if (
                len(situation) > MAX_EXPRESSION_CHARS
                or len(style) > MAX_EXPRESSION_CHARS
            ):
                return error_response(
                    f"情境和表达都不能超过 {MAX_EXPRESSION_CHARS} 个字"
                )
        featured = payload.get("featured")
        group_id = self.store.update(
            expression_id,
            situation,
            style,
            None if featured is None else bool(featured),
        )
        if group_id is None:
            return error_response("这一条已经不在了", status_code=404)
        if situation is not None:
            vectors = await self._embed([embed_text(situation, style)], retry=True)
            if vectors:
                self.store.set_vector(expression_id, vectors[0], self._embedder()[1])
        return json_response({"saved": True})

    async def api_delete(self):
        payload = await request.json(default={})
        ids = [int(value) for value in payload.get("ids") or [] if str(value).isdigit()]
        return json_response({"deleted": self.store.delete(ids)})

    async def api_learn(self):
        payload = await request.json(default={})
        group_id = str(payload.get("group") or "")
        if group_id not in self._ids("learn_groups"):
            return error_response("这个群不在「学习的群」里")
        result = await self.learn(group_id)
        if "error" in result:
            return error_response(result["error"])
        return json_response(result)

    async def api_rebuild(self):
        payload = await request.json(default={})
        group_id = str(payload.get("group") or "")
        provider, model = self._embedder()
        if provider is None:
            return error_response("没有配置可用的嵌入模型")
        if group_id in self.rebuilding:
            return json_response({"started": False})
        self.rebuilding[group_id] = "0"
        self._spawn(self._rebuild(group_id, model))
        return json_response({"started": True})

    async def _rebuild(self, group_id: str, model: str) -> None:
        """Embed every expression of a group that has no vector from the model."""
        done = 0
        try:
            while True:
                rows = self.store.without_vector(group_id, model, REBUILD_BATCH)
                if not rows:
                    break
                vectors = await self._embed(
                    [embed_text(row["situation"], row["style"]) for row in rows],
                    retry=True,
                )
                if not vectors:
                    break
                for row, vector in zip(rows, vectors, strict=True):
                    self.store.set_vector(row["id"], vector, model)
                done += len(rows)
                self.rebuilding[group_id] = str(done)
        finally:
            self.rebuilding.pop(group_id, None)
            logger.info(f"Rebuilt {done} expression vectors in group {group_id}")

    async def api_clear(self):
        payload = await request.json(default={})
        group_id = str(payload.get("group") or "")
        if not group_id:
            return error_response("缺少群号")
        return json_response({"deleted": self.store.clear_group(group_id)})
