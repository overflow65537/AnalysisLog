"""
astrbot_plugin_analysislog
==========================

监听 QQ 群中用户的求助关键词，自动定位最近 N 分钟内该用户上传的日志文件
（含 .zip 自动解压），调用 LLM 结合 MAA_Punish（法奥斯之矛）技能给出
"问题原因 + 解决方案"。也支持管理员通过 /analysis @某人 手动触发。

适用平台: 仅 aiocqhttp（QQ OneBot），仅群聊。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import random
import shutil
import zipfile
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


@dataclass
class UserCache:
    """单用户的文件记录列表（按时间正序追加）"""

    records: List[LogRecord] = field(default_factory=list)


@register(
    "astrbot_plugin_log_analysis",
    "overflow65537",
    "自动分析 maafw 日志并回复用户解决方案",
    "v0.1.0",
    "https://github.com/overflow65537/AnalysisLog",
)
class AnalysisLogPlugin(Star):
    """法奥斯之矛日志助手"""

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

        # 用户配置的 MAA_Punish 源码本地路径（仅读取，不修改）
        self.source_repo_path: Optional[Path] = None

        # 插件根目录（用于读取 skills/）
        self.plugin_dir: Path = Path(__file__).parent.resolve()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self):
        logger.info("[AnalysisLog] 插件已加载，开始监听 QQ 群日志求助")
        self._resolve_source_repo()
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
        logger.info("[AnalysisLog] 插件已卸载")

    # ------------------------------------------------------------------ #
    # 入口 1：监听所有群消息（记录文件上传 + 关键词触发）
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

        # 1) 收集消息中的 File 组件
        files = self._extract_file_components(event)
        if files:
            for f in files:
                if self._match_log_filename(f.name or ""):
                    await self._record_file(group_id, user_id, f)

        # 2) 关键词触发（命令消息交给 /analysis，自己不处理）
        message_str = (event.message_str or "").strip()
        if not message_str or message_str.startswith("/"):
            return

        if not self._match_keyword(message_str):
            return

        # 冷却
        key = (group_id, user_id)
        now = time.time()
        last = self._last_trigger.get(key, 0.0)
        cooldown = int(self.config.get("cooldown_seconds", 60))
        if now - last < cooldown:
            logger.debug(f"[AnalysisLog] 冷却中，跳过：{key}")
            return
        self._last_trigger[key] = now

        async for r in self._handle_analysis(event, target_user_id=user_id):
            yield r

    # ------------------------------------------------------------------ #
    # 入口 2：/analysis @某人 手动触发（仅群管理员）
    # ------------------------------------------------------------------ #

    @filter.command("analysis")
    async def cmd_analysis(self, event: AstrMessageEvent):
        """/analysis @某人 — 手动分析该用户最近 10 分钟内上传的日志"""
        if not self.config.get("enabled", True):
            return
        if event.get_platform_name() != PLATFORM_AIOCQHTTP:
            yield event.plain_result("本指令仅支持 QQ (OneBot) 平台")
            return
        if not event.get_group_id():
            yield event.plain_result("本指令仅可在群聊中使用")
            return

        # 仅群管理员/群主
        if self.config.get("admin_only_command", True):
            ok = await self._is_group_admin(event)
            if not ok:
                yield event.plain_result("仅群管理员/群主可使用此指令")
                return

        target_uid = self._extract_at_target(event)
        if not target_uid:
            # 没 @，则默认分析发起者自己
            target_uid = event.get_sender_id()

        async for r in self._handle_analysis(event, target_user_id=str(target_uid), manual=True):
            yield r

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def _handle_analysis(
        self,
        event: AstrMessageEvent,
        target_user_id: str,
        manual: bool = False,
    ):
        group_id = event.get_group_id() or ""
        record = self._find_latest_record(group_id, target_user_id)

        if record is None:
            # 未找到日志 → 随机催促
            nudges = self.config.get("nudge_replies") or []
            if not nudges:
                nudges = ["请先把日志文件发到群里"]
            text = random.choice(list(nudges))
            yield event.chain_result(self._mention(target_user_id) + [Plain(" " + text)])
            return

        # 先回"正在分析中"
        analyzing = self.config.get("analyzing_reply", "已找到你的日志，正在分析中，请稍候...")
        yield event.chain_result(self._mention(target_user_id) + [Plain(" " + analyzing)])

        # 下载（如尚未下载）+ 解压（若 zip）
        try:
            log_payload = await self._prepare_log_payload(record)
        except Exception as e:
            logger.exception("[AnalysisLog] 准备日志失败")
            yield event.chain_result(
                self._mention(target_user_id)
                + [Plain(f" 日志处理失败：{e}")]
            )
            return

        if not log_payload.strip():
            yield event.chain_result(
                self._mention(target_user_id)
                + [Plain(" 日志内容为空，无法分析")]
            )
            return

        # 调用 LLM
        try:
            conclusion = await self._call_llm(log_payload)
        except Exception as e:
            logger.exception("[AnalysisLog] LLM 调用失败")
            yield event.chain_result(
                self._mention(target_user_id)
                + [Plain(f" 分析失败：{e}")]
            )
            return

        if not conclusion:
            yield event.chain_result(
                self._mention(target_user_id)
                + [Plain(" 分析未返回有效结论，请稍后再试")]
            )
            return

        yield event.chain_result(
            self._mention(target_user_id) + [Plain("\n" + conclusion.strip())]
        )

    # ------------------------------------------------------------------ #
    # 文件处理
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
            file_name=f.name or "unknown",
            file_url=f.url or "",
            timestamp=time.time(),
        )
        async with self._lock:
            cache = self._caches.setdefault((group_id, user_id), UserCache())
            cache.records.append(rec)
        logger.info(f"[AnalysisLog] 记录群文件：g={group_id} u={user_id} name={rec.file_name}")

    def _find_latest_record(self, group_id: str, user_id: str) -> Optional[LogRecord]:
        cache = self._caches.get((group_id, user_id))
        if not cache or not cache.records:
            return None
        lookback = int(self.config.get("lookback_minutes", 10)) * 60
        now = time.time()
        valid = [r for r in cache.records if now - r.timestamp <= lookback]
        if not valid:
            return None
        return valid[-1]  # 最新一份

    async def _prepare_log_payload(self, record: LogRecord) -> str:
        """下载（若未下载）+ 解压（若 zip）+ 拼接日志文本。"""
        local_path = record.local_path
        if not local_path or not os.path.exists(local_path):
            local_path = await self._download(record)
            record.local_path = local_path

        max_single = int(self.config.get("max_log_chars", 30000))

        if local_path.lower().endswith(".zip"):
            extract_dir = local_path + "_extracted"
            os.makedirs(extract_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(local_path, "r") as zf:
                    zf.extractall(extract_dir)
            except zipfile.BadZipFile:
                return ""

            payload = self._collect_logs_from_dir(extract_dir, max_single)
            return payload
        else:
            text = self._read_text_tail(local_path, max_single)
            header = f"===== FILE: {os.path.basename(local_path)} =====\n"
            return header + text

    def _collect_logs_from_dir(self, root: str, per_file_cap: int) -> str:
        """递归收集 .log 文本，优先级 gui > custom > maa > 其它。不再设总上限，每文件受 per_file_cap 限制。"""
        priority = {"gui.log": 0, "custom.log": 1, "maa.log": 2}
        candidates: List[Tuple[int, str]] = []  # (prio, fullpath)
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(".log"):
                    continue
                p = priority.get(fn.lower(), 10)
                candidates.append((p, os.path.join(dirpath, fn)))
        candidates.sort(key=lambda x: x[0])

        out_parts: List[str] = []
        for _, fp in candidates:
            rel = os.path.relpath(fp, root)
            txt = self._read_text_tail(fp, per_file_cap)
            block = f"===== FILE: {rel} =====\n{txt}\n"
            out_parts.append(block)
        return "".join(out_parts)

    def _read_text_tail(self, path: str, cap: int) -> str:
        """读文件尾部最多 cap 个字符（日志关键证据通常在末尾）。"""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > cap * 4:
                    fh.seek(-cap * 4, os.SEEK_END)
                data = fh.read()
            text = data.decode("utf-8", errors="ignore")
            if len(text) > cap:
                text = text[-cap:]
            return text
        except Exception as e:
            logger.warning(f"[AnalysisLog] 读取失败 {path}: {e}")
            return ""

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
        logger.info(f"[AnalysisLog] 已下载到 {local}")
        return str(local)

    # ------------------------------------------------------------------ #
    # 匹配 / 权限 / 提及
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
        # substring
        return any(k in text for k in kws)

    def _match_log_filename(self, name: str) -> bool:
        patterns = self.config.get("file_name_patterns") or []
        for pat in patterns:
            try:
                if re.search(pat, name):
                    return True
            except re.error:
                continue
        return False

    def _extract_at_target(self, event: AstrMessageEvent) -> Optional[str]:
        for comp in event.get_messages() or []:
            if isinstance(comp, At):
                uid = str(comp.qq) if hasattr(comp, "qq") else None
                if uid and uid != event.get_self_id():
                    return uid
        return None

    def _mention(self, user_id: str) -> List:
        # 群里用 At 提醒目标
        return [At(qq=user_id)]

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """通过 OneBot get_group_member_info 判断发送者是否是管理员/群主。"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            if not isinstance(event, AiocqhttpMessageEvent):
                return False
            client = event.bot
            info = await client.call_action(
                action="get_group_member_info",
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                no_cache=False,
            )
            role = (info or {}).get("role", "member")
            return role in ("admin", "owner")
        except Exception as e:
            logger.warning(f"[AnalysisLog] 获取群成员角色失败：{e}")
            return False

    # ------------------------------------------------------------------ #
    # LLM
    # ------------------------------------------------------------------ #

    async def _call_llm(self, log_payload: str) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("当前无可用 LLM Provider，请在 AstrBot 中配置")

        system_prompt = self._build_system_prompt()
        user_prompt = (
            "以下是用户上传的日志文件完整内容：\n\n"
            f"{log_payload}\n\n"
            "请严格按要求输出最终结论。"
        )

        resp = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
            session_id=None,
            contexts=[],
        )
        return (resp.completion_text or "").strip()

    def _build_system_prompt(self) -> str:
        rel = self.config.get("skill_relative_path", "skills/maa-punish-log-analysis/SKILL.md")
        skill_path = self.plugin_dir / rel
        skill_text = ""
        if skill_path.exists():
            try:
                skill_text = skill_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"[AnalysisLog] 读取 SKILL.md 失败：{e}")
        else:
            logger.warning(f"[AnalysisLog] SKILL.md 不存在：{skill_path}")

        suffix = self.config.get("system_prompt_suffix", "")

        # 注入源码路径，供 LLM 按需对照
        if self.source_repo_path and self.source_repo_path.exists():
            repo_block = (
                "\n\n## 上游运行时注入\n"
                f"MAA_Punish 源码位置：{self.source_repo_path}\n"
                "（你可以基于该路径检索 assets/、agent/、pipeline、interface.json 等作为对照证据。）\n"
            )
        else:
            repo_block = (
                "\n\n## 上游运行时注入\n"
                "MAA_Punish 源码位置：（未提供，跳过源码对照，仅依据日志作答）\n"
            )

        # 可选：继承主人格风格
        persona_block = ""
        if self.config.get("use_persona_prompt", False):
            try:
                pm = self.context.persona_manager
                persona = (
                    pm.selected_default_persona_v3
                    or pm.get_persona_v3_by_id(pm.default_persona)
                )
                if persona and persona.get("prompt"):
                    persona_block = (
                        "\n\n## 默认人格指令（用于匹配主人格回复风格）\n"
                        f"{persona['prompt']}\n"
                    )
                    logger.info("[AnalysisLog] 已注入主人格提示词")
            except Exception as e:
                logger.warning(f"[AnalysisLog] 获取主人格失败：{e}")

        return (
            "你是 MAA_Punish（法奥斯之矛）日志分析助手。请严格遵循以下技能约束。\n\n"
            f"{skill_text}{repo_block}{persona_block}\n\n{suffix}"
        )

    # ------------------------------------------------------------------ #
    # 源码仓库定位（只读用户配置的本地路径）
    # ------------------------------------------------------------------ #

    def _resolve_source_repo(self) -> None:
        raw = (self.config.get("source_repo_path") or "").strip()
        if not raw:
            self.source_repo_path = None
            logger.info("[AnalysisLog] 未配置 source_repo_path，将跳过源码对照")
            return
        p = Path(raw).expanduser()
        if not p.exists() or not p.is_dir():
            self.source_repo_path = None
            logger.warning(f"[AnalysisLog] 配置的源码路径无效（不存在或非目录）：{p}")
            return
        self.source_repo_path = p.resolve()
        logger.info(f"[AnalysisLog] 已绑定 MAA_Punish 源码路径：{self.source_repo_path}")

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
            logger.exception("[AnalysisLog] 清理循环异常")

    async def _cleanup_once(self):
        lookback = int(self.config.get("lookback_minutes", 10)) * 60
        keep_disk_for = lookback + 86400  # 窗口外 + 1 天后删盘
        now = time.time()

        async with self._lock:
            for key, cache in list(self._caches.items()):
                cache.records = [r for r in cache.records if now - r.timestamp <= lookback]
                if not cache.records:
                    self._caches.pop(key, None)

        # 清盘
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
                            if age > keep_disk_for:
                                if entry.is_file():
                                    entry.unlink(missing_ok=True)
                                else:
                                    shutil.rmtree(entry, ignore_errors=True)
                        except Exception:
                            pass
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("[AnalysisLog] 磁盘清理异常")
