"""
astrbot_plugin_log_forward
==========================

监听 QQ 群中用户的求助关键词，自动定位该用户最近上传的日志文件，
并连同该用户最近 N 条群聊天记录，以合并转发的形式发送给指定的接收人（私聊 UMO/QQ 号）。

适用平台: 仅 aiocqhttp（QQ OneBot），仅群聊。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import File, At, Plain


PLATFORM_AIOCQHTTP = "aiocqhttp"


@dataclass
class LogRecord:
    """一条已记录的群文件上传"""

    user_id: str
    group_id: str
    file_name: str
    file_url: str
    timestamp: float
    local_path: Optional[str] = None  # 下载后本地路径
    forwarded: bool = False  # 是否已转发过，避免对同一份日志重复转发


@dataclass
class UserCache:
    """单用户的文件记录列表（按时间正序追加）"""

    records: List[LogRecord] = field(default_factory=list)


@register(
    "astrbot_plugin_log_analysis",
    "overflow65537",
    "监听群求助关键词，将用户日志与最近聊天记录合并转发给指定接收人",
    "v0.2.0",
    "https://github.com/overflow65537/AnalysisLog",
)
class AnalysisLogPlugin(Star):
    """法奥斯之矛日志转发助手"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # {(group_id, user_id): UserCache}
        self._caches: Dict[Tuple[str, str], UserCache] = {}
        # {(group_id, user_id): last_trigger_ts}
        self._last_trigger: Dict[Tuple[str, str], float] = {}

        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

        self.data_dir: Path = StarTools.get_data_dir()
        self.cache_root: Path = self.data_dir / "analysislog_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self):
        logger.info("[LogForward] 插件已加载，开始监听 QQ 群日志求助")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def terminate(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._caches.clear()
        self._last_trigger.clear()
        logger.info("[LogForward] 插件已卸载")

    # ------------------------------------------------------------------ #
    # 入口：监听所有群消息（记录文件上传 + 关键词触发）
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=50)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        if event.get_platform_name() != PLATFORM_AIOCQHTTP:
            return

        group_id = event.get_group_id() or ""
        user_id = event.get_sender_id() or ""
        if not group_id or not user_id:
            return

        # 1) 收集消息中的 File 组件（匹配日志文件名的才记录）
        files = self._extract_file_components(event)
        if files:
            for f in files:
                if self._match_log_filename(f.name or ""):
                    await self._record_file(group_id, user_id, f)

        # 2) 关键词触发（命令消息忽略）
        message_str = (event.message_str or "").strip()
        if not message_str or message_str.startswith("/"):
            return

        if not self._match_keyword(message_str):
            return

        # 冷却（加锁，避免同一用户多事件并发竞态导致重复触发）
        key = (group_id, user_id)
        cooldown = int(self.config.get("cooldown_seconds", 60))
        async with self._lock:
            now = time.time()
            last = self._last_trigger.get(key, 0.0)
            if now - last < cooldown:
                logger.debug(f"[LogForward] 冷却中，跳过：{key}")
                return
            self._last_trigger[key] = now

        async for r in self._handle_forward(event, target_user_id=user_id):
            yield r

    # ------------------------------------------------------------------ #
    # 主流程：转发
    # ------------------------------------------------------------------ #

    async def _handle_forward(self, event: AstrMessageEvent, target_user_id: str):
        group_id = event.get_group_id() or ""

        # 接收人 UMO（QQ 号）
        receiver = str(self.config.get("receiver_uin", "") or "").strip()
        if not receiver.isdigit():
            logger.warning("[LogForward] 未正确配置 receiver_uin（接收人 QQ 号），跳过转发")
            yield event.chain_result(
                self._mention(target_user_id)
                + [Plain(" 未配置接收人，请联系管理员设置 receiver_uin")]
            )
            return

        # 定位最近一份未转发的日志
        record = self._find_latest_record(group_id, target_user_id)
        if record is None:
            nudges = self.config.get("nudge_replies") or ["请先把日志文件发到群里"]
            text = random.choice(list(nudges))
            yield event.chain_result(self._mention(target_user_id) + [Plain(" " + text)])
            return

        # 回复"已收到"提示（列表随机）
        received = self.config.get("received_replies") or ["已收到你的日志，正在转交给管理员..."]
        yield event.chain_result(
            self._mention(target_user_id) + [Plain(" " + random.choice(list(received)))]
        )

        # 拉取该用户最近 N 条聊天记录
        try:
            history_nodes = await self._fetch_user_history_nodes(event, target_user_id)
        except Exception as e:
            logger.exception("[LogForward] 拉取群历史失败")
            history_nodes = []

        # 下载日志文件
        try:
            local_path = await self._ensure_downloaded(record)
        except Exception as e:
            logger.exception("[LogForward] 日志下载失败")
            yield event.chain_result(
                self._mention(target_user_id) + [Plain(f" 日志下载失败：{e}")]
            )
            return

        # 组装合并转发并发送
        try:
            await self._send_forward(
                event, receiver, group_id, target_user_id, history_nodes, record, local_path
            )
        except Exception as e:
            logger.exception("[LogForward] 合并转发失败")
            yield event.chain_result(
                self._mention(target_user_id) + [Plain(f" 转发给管理员失败：{e}")]
            )
            return

        record.forwarded = True
        done = self.config.get("done_replies") or ["已转交给管理员，请耐心等待回复~"]
        yield event.chain_result(
            self._mention(target_user_id) + [Plain(" " + random.choice(list(done)))]
        )

    # ------------------------------------------------------------------ #
    # OneBot: 拉取群历史 / 发送合并转发
    # ------------------------------------------------------------------ #

    def _get_client(self, event: AstrMessageEvent):
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        if not isinstance(event, AiocqhttpMessageEvent):
            raise RuntimeError("当前事件非 aiocqhttp，无法调用 OneBot API")
        return event.bot

    async def _fetch_user_history_nodes(
        self, event: AstrMessageEvent, target_user_id: str
    ) -> List[dict]:
        """拉取群历史消息，筛出目标用户最近 N 条，构造为合并转发 node 列表。"""
        client = self._get_client(event)
        group_id = int(event.get_group_id())
        want = int(self.config.get("history_count", 15))
        # 多拉一些再筛，OneBot 一次最多 ~20 条，循环向前翻页
        collected: List[dict] = []
        seq = 0  # 0 表示最新
        pages = 0
        max_pages = int(self.config.get("history_max_pages", 10))
        while len(collected) < want and pages < max_pages:
            pages += 1
            try:
                params = {"group_id": group_id, "count": 20}
                if seq:
                    params["message_seq"] = seq
                resp = await client.call_action(
                    action="get_group_msg_history", **params
                )
            except Exception as e:
                logger.warning(f"[LogForward] get_group_msg_history 失败：{e}")
                break
            msgs = (resp or {}).get("messages", []) or []
            if not msgs:
                break
            # 记录本页最早一条的 seq，用于翻页
            first = msgs[0]
            seq = first.get("message_seq") or first.get("real_id") or 0
            # 从新到旧筛目标用户
            for m in reversed(msgs):
                sender = (m.get("sender") or {})
                uid = str(sender.get("user_id", ""))
                if uid != str(target_user_id):
                    continue
                collected.append(m)
                if len(collected) >= want:
                    break
            if not seq:
                break

        collected = list(reversed(collected[:want]))  # 时间正序
        nodes: List[dict] = []
        for m in collected:
            sender = (m.get("sender") or {})
            nick = sender.get("nickname") or sender.get("card") or str(target_user_id)
            content = m.get("message")
            if content is None:
                content = m.get("raw_message") or ""
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": str(nick),
                        "uin": str(target_user_id),
                        "content": content,
                    },
                }
            )
        logger.info(f"[LogForward] 收集到目标用户历史 {len(nodes)} 条")
        return nodes

    async def _send_forward(
        self,
        event: AstrMessageEvent,
        receiver: str,
        group_id: str,
        target_user_id: str,
        history_nodes: List[dict],
        record: LogRecord,
        local_path: str,
    ):
        """组装合并转发并通过私聊发送给接收人。"""
        client = self._get_client(event)
        self_id = str(event.get_self_id() or "10000")

        nodes: List[dict] = []
        # 头部说明
        header = (
            f"📋 日志求助转发\n"
            f"群号：{group_id}\n"
            f"用户：{target_user_id}\n"
            f"日志：{record.file_name}"
        )
        nodes.append(self._text_node(self_id, "日志助手", header))

        # 聊天记录
        if history_nodes:
            nodes.append(self._text_node(self_id, "日志助手", "—— 最近聊天记录 ——"))
            nodes.extend(history_nodes)
        else:
            nodes.append(self._text_node(self_id, "日志助手", "（未能拉取到聊天记录）"))

        # 日志文件节点（以 file 段发送）
        abs_path = os.path.abspath(local_path)
        file_seg = {
            "type": "file",
            "data": {"file": f"file:///{abs_path}", "name": record.file_name},
        }
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "日志助手",
                    "uin": self_id,
                    "content": [file_seg],
                },
            }
        )

        await client.call_action(
            action="send_private_forward_msg",
            user_id=int(receiver),
            messages=nodes,
        )
        logger.info(f"[LogForward] 已合并转发给接收人 {receiver}")

    def _text_node(self, uin: str, name: str, text: str) -> dict:
        return {
            "type": "node",
            "data": {
                "name": name,
                "uin": str(uin),
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }

    # ------------------------------------------------------------------ #
    # 文件记录 / 查找 / 下载
    # ------------------------------------------------------------------ #

    def _extract_file_components(self, event: AstrMessageEvent) -> List[File]:
        result: List[File] = []
        for comp in event.get_messages() or []:
            if isinstance(comp, File):
                result.append(comp)
        return result

    async def _record_file(self, group_id: str, user_id: str, f: File):
        rec = LogRecord(
            user_id=user_id,
            group_id=group_id,
            file_name=f.name or "log",
            file_url=getattr(f, "url", "") or "",
            timestamp=time.time(),
        )
        async with self._lock:
            cache = self._caches.setdefault((group_id, user_id), UserCache())
            cache.records.append(rec)
        logger.info(f"[LogForward] 记录群文件：g={group_id} u={user_id} name={rec.file_name}")

    def _find_latest_record(self, group_id: str, user_id: str) -> Optional[LogRecord]:
        cache = self._caches.get((group_id, user_id))
        if not cache or not cache.records:
            return None
        lookback = int(self.config.get("lookback_minutes", 10)) * 60
        now = time.time()
        valid = [
            r for r in cache.records
            if now - r.timestamp <= lookback and not r.forwarded
        ]
        if not valid:
            return None
        return valid[-1]

    async def _ensure_downloaded(self, record: LogRecord) -> str:
        local_path = record.local_path
        if local_path and os.path.exists(local_path):
            return local_path
        local_path = await self._download(record)
        record.local_path = local_path
        return local_path

    async def _download(self, record: LogRecord) -> str:
        if not record.file_url:
            raise RuntimeError("文件 URL 为空，无法下载")
        sub_dir = self.cache_root / record.group_id / record.user_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-]+", "_", record.file_name)
        local = sub_dir / f"{int(record.timestamp)}_{safe_name}"

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(record.file_url) as resp:
                resp.raise_for_status()
                with open(local, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        fh.write(chunk)
        logger.info(f"[LogForward] 已下载到 {local}")
        return str(local)

    # ------------------------------------------------------------------ #
    # 匹配 / 提及
    # ------------------------------------------------------------------ #

    def _match_keyword(self, text: str) -> bool:
        kws = self.config.get("keywords") or []
        if not kws:
            return False
        mode = self.config.get("keyword_mode", "substring")
        if mode == "regex":
            for pat in kws:
                try:
                    if re.search(pat, text):
                        return True
                except re.error:
                    continue
            return False
        return any(k in text for k in kws)

    def _match_log_filename(self, name: str) -> bool:
        patterns = self.config.get("file_name_patterns") or []
        if not patterns:
            return False
        for pat in patterns:
            try:
                if re.search(pat, name):
                    return True
            except re.error:
                continue
        return False

    def _mention(self, user_id: str) -> List:
        return [At(qq=user_id)]

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #

    async def _cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(300)
                await self._cleanup_once()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[LogForward] 清理循环异常")

    async def _cleanup_once(self):
        lookback = int(self.config.get("lookback_minutes", 10)) * 60
        keep_disk_for = lookback + 86400
        now = time.time()

        async with self._lock:
            for key, cache in list(self._caches.items()):
                cache.records = [r for r in cache.records if now - r.timestamp <= lookback]
                if not cache.records:
                    self._caches.pop(key, None)

        try:
            for group_dir in self.cache_root.iterdir():
                if not group_dir.is_dir():
                    continue
                for user_dir in group_dir.iterdir():
                    if not user_dir.is_dir():
                        continue
                    for entry in user_dir.iterdir():
                        try:
                            age = now - entry.stat().st_mtime
                            if age > keep_disk_for and entry.is_file():
                                entry.unlink(missing_ok=True)
                        except Exception:
                            pass
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("[LogForward] 磁盘清理异常")
