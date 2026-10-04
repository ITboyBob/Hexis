"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Avatar } from "./avatar";
import { compose } from "../../lib/avatar-preview/composition";
import { EMOTIONS, WORDSET_VERSION } from "../../lib/avatar-preview/contracts";
import type { Emotion, Event, Identity, Payload } from "../../lib/avatar-preview/contracts";
import { EmotionWindowHost } from "../../lib/avatar-preview/emotion-window";
import { Effects } from "../../lib/avatar-preview/effects";
import type { Character } from "../../lib/avatar-preview/engine";
import { activeTasks, applyEvent, eventReceipt, identityKey, initial } from "../../lib/avatar-preview/projection";
import type { State } from "../../lib/avatar-preview/projection";
import { parseInjected, eventSchema } from "../../lib/avatar-preview/validation";
import { scenarioById, scenarios } from "../../lib/avatar-preview/scenarios";
import type { ScenarioStep } from "../../lib/avatar-preview/scenarios";

const emotionDescriptions: Record<Emotion, string> = {
  neutral: "本条消息未引起明显主导情绪反应",
  happy: "欣喜、愉悦、满足",
  excited: "强烈兴奋、期待",
  curious: "探索兴趣",
  surprised: "意外、变化、新奇引起的惊讶",
  confused: "Hexis 对内容困惑",
  suspicious: "有依据的警惕、怀疑",
  angry: "冒犯、不公、边界侵犯引起的恼怒",
  sad: "悲伤、失落、受伤、遗憾",
  scared: "有依据危险、预期伤害引起的害怕",
  proud: "自身成就、价值肯定引起的自豪",
  shy: "关注、亲近、赞美引起的羞涩",
  bored: "内容乏味引起的无聊",
  playful: "玩兴、轻松打趣的感受",
};

type Diagnostic = { event?: Event; accepted: boolean; reason: string; source: string; time: string };
const SAMPLE_LIMIT = 250;
const RECORD_LIMIT = 10000;

function newId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `preview-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function taskSource(source: string): string {
  return source === "manual" ? "手工" : source === "real" ? "真实服务" : "模拟";
}

function taskPhase(phase: string): string {
  const labels: Record<string, string> = {
    queued: "排队中", running: "运行中", waiting_user: "等待用户", cancelling: "取消中",
    unknown: "状态未知", completed: "已完成", failed: "失败", cancelled: "已取消",
  };
  return labels[phase] ?? phase;
}

export default function AvatarPreviewPage() {
  // Server and first client render use the same placeholder. A real session starts after mount.
  const [state, setState] = useState<State>(() => initial("preview-uninitialized"));
  const stateRef = useRef(state);
  const sequence = useRef(0);
  const eventNumber = useRef(0);
  const hostRef = useRef<EmotionWindowHost | null>(null);
  const pendingTimeouts = useRef<Map<string, number>>(new Map());
  const engineRef = useRef<Character | null>(null);
  const effectsRef = useRef<Effects | null>(null);
  const pendingEffects = useRef<Array<{ id: string; kind: "bounce" | "burst"; duration_ms: number }>>([]);
  const eventsRef = useRef<Event[]>([]);
  const diagnosticsRef = useRef<Diagnostic[]>([]);
  const lateVisualCallbacks = useRef<Array<() => void>>([]);
  const lateModelCallbacks = useRef<Array<() => void>>([]);
  const dispatchRef = useRef<(payload: Payload, stream?: string) => State>(() => stateRef.current);
  const alive = useRef(true);
  const [ready, setReady] = useState(false);
  const [avatarMount, setAvatarMount] = useState(0);
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [dropped, setDropped] = useState(0);
  const [recordComplete, setRecordComplete] = useState(false);
  const recordCompleteRef = useRef(false);
  const [message, setMessage] = useState("");
  const [simulatedLabel, setSimulatedLabel] = useState<Emotion>("curious");
  const [contextSource, setContextSource] = useState<"fixture" | "db">("fixture");
  const [editor, setEditor] = useState("");
  const [error, setError] = useState("");
  const [size, setSize] = useState<number | null>(null);
  const sizeRef = useRef<number | null>(null);
  const [sizeSource, setSizeSource] = useState("等待真实 geometry");
  const [motionLimit, setMotionLimit] = useState(0);
  const motionLimitRef = useRef(0);
  const [effectDuration, setEffectDuration] = useState("");
  const [effectGap, setEffectGap] = useState("");
  const effectGapRef = useRef("");
  const [effectId, setEffectId] = useState("");
  const [effectSnapshot, setEffectSnapshot] = useState<{ active?: string; queued: string[]; min_gap_ms: number }>({ queued: [], min_gap_ms: 0 });
  const [scenarioId, setScenarioId] = useState("C01");
  const [scenarioIndex, setScenarioIndex] = useState(0);
  const [scenarioSteps, setScenarioSteps] = useState<ScenarioStep[]>([]);
  const [scenarioBusy, setScenarioBusy] = useState(false);
  const [scenarioNotice, setScenarioNotice] = useState("");
  const [previousFocus, setPreviousFocus] = useState<string | undefined>(undefined);
  const previousFocusRef = useRef<string | undefined>(undefined);
  const [recordingMode, setRecordingMode] = useState("抽样诊断：最近250条");
  const composition = compose(state, previousFocus, motionLimit);

  const appendDiagnostic = useCallback((entry: Diagnostic) => {
    const list = diagnosticsRef.current;
    list.push(entry);
    if (list.length > SAMPLE_LIMIT) {
      list.shift();
      setDropped((value) => value + 1);
    }
    setDiagnostics([...list]);
  }, []);

  const ingest = useCallback((raw: unknown, injected = false): State => {
    let event = injected ? parseInjected(typeof raw === "string" ? raw : JSON.stringify(raw)) : eventSchema.parse(raw) as Event;
    if (event.session_id !== stateRef.current.session_id) throw new Error("事件会话与当前预览不匹配；复制当前事件或显式重开会话");
    if (stateRef.current.dead) {
      appendDiagnostic({ event, accepted: false, reason: "预览已销毁；显式重开会话后再发送", source: "injected", time: new Date().toISOString() });
      return stateRef.current;
    }
    const payload = event.payload;
    const initialReceipt = eventReceipt(stateRef.current, event);
    if (payload.type === "input_committed" && initialReceipt.accepted) {
      const windowKey = identityKey(payload.identity);
      const timeoutMs = pendingTimeouts.current.get(windowKey) ?? 30000;
      pendingTimeouts.current.delete(windowKey);
      hostRef.current?.open(payload.identity, timeoutMs);
      const identity = payload.identity;
      const generation = stateRef.current.visual_generation;
      lateVisualCallbacks.current.push(() => {
        if (stateRef.current.dead || stateRef.current.visual_generation !== generation) return;
        engineRef.current?.resetVisual();
      });
      lateModelCallbacks.current.push(() => {
        const result = hostRef.current?.acceptResult(identity);
        const fact = result?.fact ?? hostRef.current?.current(identity);
        dispatchRef.current({ type: "emotion", identity, wordset_version: WORDSET_VERSION, status: "valid",
          label: "curious", source: "simulated", visual_window: fact });
      });
    }
    if (payload.type === "emotion" && payload.status === "valid") {
      const identity = payload.identity;
      const isCurrentIdentity = stateRef.current.current !== undefined
        && identityKey(stateRef.current.current) === identityKey(identity);
      if (isCurrentIdentity && payload.wordset_version === WORDSET_VERSION
        && initialReceipt.code !== "duplicate" && hostRef.current?.current(identity)) {
        const decision = hostRef.current.acceptResult(identity);
        event = eventSchema.parse({ ...event, payload: { ...payload, visual_window: decision.fact } }) as Event;
      }
    }
    if (payload.type === "control" && initialReceipt.accepted) {
      if (payload.action === "reset") effectsRef.current?.cancelEffects();
      if (payload.action === "reduced_motion") effectsRef.current?.setReduced(Boolean(payload.enabled));
      if (payload.action === "stop_effect") effectsRef.current?.stop(payload.effect_id ?? "");
      if (payload.action === "destroy") {
        hostRef.current?.dispose();
        effectsRef.current?.destroy();
        effectsRef.current = null;
      }
    }
    const before = stateRef.current;
    const { state: next, receipt } = applyEvent(before, event);
    stateRef.current = next;
    setState(next);
    const focus = compose(next, previousFocusRef.current, motionLimitRef.current).focus;
    previousFocusRef.current = focus;
    setPreviousFocus(focus);
    if (recordCompleteRef.current && eventsRef.current.length < RECORD_LIMIT) eventsRef.current.push(event);
    else if (!recordCompleteRef.current) {
      if (eventsRef.current.length >= SAMPLE_LIMIT) eventsRef.current.shift();
      eventsRef.current.push(event);
    } else setRecordingMode("完整录制达到10000条上限；超出部分未记录");
    appendDiagnostic({
      event,
      accepted: receipt.accepted,
      reason: receipt.reason,
      source: event.payload.type === "task" ? taskSource(event.payload.task.source)
        : event.payload.type === "playback" ? taskSource(event.payload.feedback.source)
          : "source" in event.payload ? taskSource(event.payload.source) : "本地宿主",
      time: new Date().toISOString(),
    });
    return next;
  }, [appendDiagnostic]);

  const dispatch = useCallback((payload: Payload, stream = "preview-local"): State => {
    const event = eventSchema.parse({
      event_id: `preview-event-${++eventNumber.current}-${newId()}`,
      session_id: stateRef.current.session_id,
      stream_id: stream,
      producer_seq: ++sequence.current,
      payload,
    }) as Event;
    return ingest(event);
  }, [ingest]);

  useEffect(() => { dispatchRef.current = dispatch; }, [dispatch]);

  const createLocalSession = useCallback((sessionId: string) => {
    const fresh = initial(sessionId);
    stateRef.current = fresh;
    setState(fresh);
    sequence.current = 0;
    eventNumber.current = 0;
    eventsRef.current = [];
    diagnosticsRef.current = [];
    setDiagnostics([]);
    setDropped(0);
    hostRef.current?.dispose();
    pendingTimeouts.current.clear();
    hostRef.current = new EmotionWindowHost({
      owner: "local",
      onChange: (identity, fact) => {
        if (alive.current) dispatch({ type: "visual_window", identity, fact }, "visual-window-local");
      },
    });
    lateVisualCallbacks.current = [];
    lateModelCallbacks.current = [];
    setScenarioSteps([]);
    setPreviousFocus(undefined);
    previousFocusRef.current = undefined;
  }, [dispatch]);

  useEffect(() => {
    alive.current = true;
    let mounted = true;
    queueMicrotask(() => {
      if (!mounted) return;
      createLocalSession(newId());
      setReady(true);
    });
    return () => {
      mounted = false;
      alive.current = false;
      hostRef.current?.dispose();
      hostRef.current = null;
      effectsRef.current?.destroy();
      effectsRef.current = null;
      engineRef.current = null;
    };
  }, [createLocalSession]);

  const openInput = useCallback((identity: Identity, timeoutMs = 30000) => {
    pendingTimeouts.current.set(identityKey(identity), timeoutMs);
    return dispatch({ type: "input_committed", identity, visual_owner: "local" });
  }, [dispatch]);

  const nextIdentity = useCallback((): Identity => ({
    session_id: stateRef.current.session_id,
    input_turn_id: newId(),
    input_order: (stateRef.current.current?.input_order ?? 0) + 1,
    stimulus_revision: 1,
  }), []);

  const sendEmotion = useCallback((identity: Identity, label: Emotion, source: "manual" | "simulated") => {
    const decision = hostRef.current?.acceptResult(identity);
    return dispatch({ type: "emotion", identity, wordset_version: WORDSET_VERSION, status: "valid", label, source,
      visual_window: decision?.fact ?? hostRef.current?.current(identity),
    });
  }, [dispatch]);

  const chooseLabel = useCallback((label: Emotion) => {
    const identity = nextIdentity();
    openInput(identity);
    sendEmotion(identity, label, "manual");
  }, [nextIdentity, openInput, sendEmotion]);

  const simulateMessage = () => {
    if (!message.trim()) {
      setError("先输入一条消息，再用显式模拟标签评估；当前不会调用模型服务");
      return;
    }
    const identity = nextIdentity();
    openInput(identity);
    dispatch({ type: "emotion", identity, wordset_version: WORDSET_VERSION, status: "pending", source: "simulated",
      reason: "用户选择本地模拟评估；消息未发送给模型" });
    dispatch({ type: "task", task: { activity_id: identity.input_turn_id, task_id: identity.input_turn_id,
      kind: "emotion_request", foreground: true, phase: "running", source: "simulated" } });
    sendEmotion(identity, simulatedLabel, "simulated");
    dispatch({ type: "task", task: { activity_id: identity.input_turn_id, task_id: identity.input_turn_id,
      kind: "emotion_request", foreground: true, phase: "completed", source: "simulated" } });
    setError("");
  };

  const runEffect = useCallback((effect: { id: string; kind: "bounce" | "burst"; duration_ms: number }) => {
    if (!effectsRef.current && engineRef.current) effectsRef.current = new Effects(engineRef.current, Number(effectGapRef.current) || 0, setEffectSnapshot);
    if (effectsRef.current) {
      effectsRef.current.setReduced(stateRef.current.reduced_motion);
      effectsRef.current.enqueue(effect);
    }
    else pendingEffects.current.push(effect);
  }, []);

  const resetVisual = useCallback(() => {
    effectsRef.current?.cancelEffects();
    dispatch({ type: "control", action: "reset" });
  }, [dispatch]);

  const destroyPreview = useCallback(() => {
    hostRef.current?.dispose();
    effectsRef.current?.destroy();
    effectsRef.current = null;
    dispatch({ type: "control", action: "destroy" });
  }, [dispatch]);

  const reopen = useCallback(() => {
    effectsRef.current?.destroy();
    effectsRef.current = null;
    pendingEffects.current = [];
    hostRef.current?.dispose();
    createLocalSession(newId());
    setError("");
    setScenarioIndex(0);
  }, [createLocalSession]);

  const rebuildAvatar = useCallback(() => {
    effectsRef.current?.destroy();
    effectsRef.current = null;
    engineRef.current = null;
    pendingEffects.current = [];
    setEffectSnapshot({ queued: [], min_gap_ms: 0 });
    dispatch({ type: "control", action: "reset" });
    setAvatarMount((value) => value + 1);
    appendDiagnostic({ accepted: true, reason: "仅重建角色渲染器；当前输入与业务资格保持不变",
      source: "本地视觉生命周期", time: new Date().toISOString() });
  }, [appendDiagnostic, dispatch]);

  const onEngineReady = useCallback((character: Character | null) => {
    if (!character) {
      effectsRef.current?.destroy();
      effectsRef.current = null;
      setEffectSnapshot({ queued: [], min_gap_ms: 0 });
      engineRef.current = null;
      return;
    }
    engineRef.current = character;
    if (sizeRef.current === null) {
      const geometryWidth = window.GROK_GEO?.viewBox.width;
      if (Number.isFinite(geometryWidth) && geometryWidth > 0) {
        sizeRef.current = geometryWidth;
        setSize(geometryWidth);
        setSizeSource("GROK_GEO.viewBox.width");
      }
    }
    for (const effect of pendingEffects.current.splice(0)) runEffect(effect);
  }, [runEffect]);

  const inject = () => {
    try {
      const event = parseInjected(editor);
      const payload = event.payload;
      if (payload.type === "visual_window") throw new Error("事件编辑器不能注入服务端资格事实");
      ingest(event, true);
      setError("");
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "事件无效；检查字段后重试";
      setError(message);
      appendDiagnostic({ accepted: false, reason: message, source: "JSON注入", time: new Date().toISOString() });
    }
  };

  const selectScenario = (id: string) => {
    setScenarioId(id);
    setScenarioIndex(0);
    const base: Identity = {
      session_id: stateRef.current.session_id,
      input_turn_id: `${id.toLowerCase()}-${newId()}`,
      input_order: (stateRef.current.current?.input_order ?? 0) + 1,
      stimulus_revision: 1,
    };
    setScenarioSteps(scenarioById(id)?.build(base) ?? []);
    setScenarioNotice(scenarioById(id)?.limitation ?? "");
  };

  const sendScenarioStep = async () => {
    const scenario = scenarioById(scenarioId);
    if (!scenario || scenarioBusy) return;
    let steps = scenarioSteps;
    if (steps.length === 0) {
      const base: Identity = { session_id: stateRef.current.session_id,
        input_turn_id: `${scenarioId.toLowerCase()}-${newId()}`,
        input_order: (stateRef.current.current?.input_order ?? 0) + 1, stimulus_revision: 1 };
      steps = scenario.build(base);
      setScenarioSteps(steps);
    }
    if (scenarioIndex >= steps.length) return;
    const step = steps[scenarioIndex];
    setScenarioBusy(true);
    try {
      await runScenarioStep(step);
      setScenarioIndex((current) => current + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "场景步骤失败；可修复输入后继续");
    } finally {
      setScenarioBusy(false);
    }
  };

  const runScenarioStep = async (step: ScenarioStep) => {
    if (step.kind === "event") {
      const payload = step.payload;
      dispatch(payload);
      return;
    }
    if (step.kind === "effect_enqueue") { runEffect(step.effect); return; }
    if (step.kind === "local_open") { pendingTimeouts.current.set(identityKey(step.identity), step.timeout_ms); return; }
    if (step.kind === "local_timeout") {
      await new Promise<void>((resolve) => setTimeout(resolve, step.timeout_ms + 2));
      return;
    }
    if (step.kind === "invalid_payload") {
      try { parseInjected(step.raw); }
      catch (cause) {
        const reason = cause instanceof Error ? cause.message : "事件无效";
        appendDiagnostic({ accepted: false, reason, source: "场景坏payload", time: new Date().toISOString() });
        setError(reason);
      }
      return;
    }
    if (step.kind === "lifecycle") {
      if (step.action === "destroy") destroyPreview();
      else if (step.action === "late_model_result") lateModelCallbacks.current.at(-1)?.();
      else lateVisualCallbacks.current.at(-1)?.();
      return;
    }
    setScenarioNotice("该场景需要显式配置预览服务并由用户发起；当前没有发出网络请求。");
  };

  const toggleReducedMotion = useCallback((enabled: boolean) => {
    effectsRef.current?.setReduced(enabled);
    dispatch({ type: "control", action: "reduced_motion", enabled });
  }, [dispatch]);

  useEffect(() => {
    if (!ready || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => toggleReducedMotion(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [ready, toggleReducedMotion]);

  const enqueueManualEffect = () => {
    const duration = Number(effectDuration);
    const gap = Number(effectGap);
    if (!effectDuration || !effectGap || !Number.isFinite(duration) || duration <= 0 || !Number.isFinite(gap) || gap < 0) {
      setError("请填写正数效果时长和非负最小间隔，再启动效果");
      return;
    }
    if (!effectsRef.current && engineRef.current) effectsRef.current = new Effects(engineRef.current, gap, setEffectSnapshot);
    effectsRef.current?.setReduced(stateRef.current.reduced_motion);
    const effect = { id: newId(), kind: "bounce" as const, duration_ms: duration };
    if (effectsRef.current) effectsRef.current.enqueue(effect); else pendingEffects.current.push(effect);
    setEffectId(effect.id);
  };

  const exportEvidence = () => {
    const evidence = {
      schema_version: "avatar-preview-evidence-v1",
      session_id: stateRef.current.session_id,
      wordset_version: WORDSET_VERSION,
      source_mode: "手工/模拟离线预览",
      recording_mode: recordComplete ? "完整轨迹（上限10000条）" : recordingMode,
      dropped_event_count: dropped,
      events: eventsRef.current,
      diagnostics: diagnosticsRef.current,
      projection: stateRef.current,
      limitation: "当前页面未接入模型服务或真实播放器；不会自动读取数据库或发起网络请求。",
    };
    const blob = new Blob([JSON.stringify(evidence, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `avatar-preview-${stateRef.current.session_id}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const visibleTasks = activeTasks(state);
  const backgroundCount = visibleTasks.filter((item) => !item.foreground).length;
  const selectedScenario = scenarioById(scenarioId);
  const scenarioLength = scenarioSteps.length || selectedScenario?.build({
    session_id: state.session_id, input_turn_id: "preview-placeholder", input_order: 0, stimulus_revision: 0,
  }).length || 0;

  return (
    <main className="avatar-preview" lang="zh-CN">
      <h1>二维 Avatar 独立预览</h1>
      <p>离线预览仅处理手工或模拟事件。服务评估、数据库读取和真实播放器都不会自动启动。</p>
      {error && <p role="alert">{error}</p>}
      {ready && !state.dead
        ? <Avatar key={`${state.session_id}:${avatarMount}`} value={composition.value} size={size ?? 0} generation={state.visual_generation} onReady={onEngineReady} />
        : <div role="status">{state.dead ? "预览已销毁；请显式重开会话" : "正在创建本地预览会话…"}</div>}

      <section aria-labelledby="status-title">
        <h2 id="status-title">当前投影</h2>
        <output data-testid="emotion-current">{state.last_valid?.label ?? "未评估"}</output>
        <p data-testid="evaluation-status">本次评估：{state.evaluation}；{state.reason}</p>
        <output data-testid="emotion-source">{state.last_valid ? taskSource(state.last_valid.source) + "标签" : "无模型结论"}</output>
        <p data-testid="session-id">会话：{state.session_id}</p>
        <p>当前身份：{state.current ? `${state.current.input_turn_id} / 输入序号${state.current.input_order} / 修订${state.current.stimulus_revision}` : "尚无有效输入"}</p>
        <p>视觉资格：{state.current ? state.visual_windows[identityKey(state.current)]?.status ?? "未开窗" : "未开窗"}</p>
        <output data-testid="visual-qualification">{state.current ? state.visual_windows[identityKey(state.current)]?.status ?? "未开窗" : "未开窗"}</output>
        <p>主要注意力：{composition.focus}；眼形：{composition.value.eye}；姿态：{composition.value.pose}；播放幅度：{composition.value.amplitude}</p>
      </section>

      <fieldset disabled={state.dead}>
        <legend>手工标签与消息</legend>
        <div className="emotion-buttons" aria-label="14项手工情绪标签">
          {EMOTIONS.map((label) => <button type="button" key={label} aria-label={`手工标签 ${label}：${emotionDescriptions[label]}`}
            title={emotionDescriptions[label]} onClick={() => chooseLabel(label)}>{label}<small> — {emotionDescriptions[label]}</small></button>)}
        </div>
        <label htmlFor="preview-message">消息文本</label>
        <textarea id="preview-message" value={message} onChange={(event) => setMessage(event.target.value)} />
        <label htmlFor="context-source">上下文来源</label>
        <select id="context-source" value={contextSource} onChange={(event) => setContextSource(event.target.value as "fixture" | "db")}>
          <option value="fixture">合成fixture</option><option value="db">已审查上下文来源（服务接入后）</option>
        </select>
        <label htmlFor="simulated-label">本地模拟结果标签（不会读取上下文或调用模型）</label>
        <select id="simulated-label" value={simulatedLabel} onChange={(event) => setSimulatedLabel(event.target.value as Emotion)}>
          {EMOTIONS.map((label) => <option key={label} value={label}>{label} — {emotionDescriptions[label]}</option>)}
        </select>
        <button type="button" onClick={simulateMessage}>在本地模拟消息标签</button>
        <button type="button" disabled>{contextSource === "fixture" ? "使用合成fixture评估（需显式接入服务）" : "使用四类上下文评估（需显式接入服务）"}</button>
        <button type="button" disabled>查询原评估（需显式服务配置）</button>
        <p>provider/model：尚未连接；一次逻辑调用：0。手工消息不会隐式发给模型。</p>
      </fieldset>

      <fieldset>
        <legend>固定场景与受控事件</legend>
        <label htmlFor="scenario-select">固定场景</label>
        <select id="scenario-select" disabled={state.dead} value={scenarioId} onChange={(event) => selectScenario(event.target.value)}>
          {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.id} — {scenario.title}</option>)}
        </select>
        <p>{selectedScenario?.limitation ?? scenarioNotice}</p>
        <button type="button" disabled={scenarioBusy || scenarioIndex >= scenarioLength || (state.dead && scenarioId !== "C08")} onClick={() => void sendScenarioStep()}>
          {scenarioBusy ? "正在执行…" : "发送下一条场景事件"}
        </button>
        <output aria-live="polite">场景进度：{Math.min(scenarioIndex, scenarioLength)}/{scenarioLength}</output>
        <label htmlFor="event-json">事件JSON</label>
        <textarea id="event-json" disabled={state.dead} value={editor} onChange={(event) => setEditor(event.target.value)} />
        <button type="button" onClick={inject} disabled={state.dead}>注入手工或模拟事件</button>
      </fieldset>

      <fieldset disabled={state.dead}>
        <legend>活动集合与来源</legend>
        <p>后台活动：{backgroundCount}</p>
        <ul data-testid="active-tasks">
          {visibleTasks.length === 0 ? <li>无活动</li> : visibleTasks.map((item) => <li key={item.activity_id}>
            {item.activity_id}；类型 {item.kind}；{item.foreground ? "前台" : "后台"}；{taskPhase(item.phase)}；{taskSource(item.source)}
          </li>)}
        </ul>
      </fieldset>

      <fieldset disabled={state.dead}>
        <legend>播放与视觉控制</legend>
        <label htmlFor="audio-file">选择本地音频（Phase 5接入前不会播放）</label>
        <input id="audio-file" type="file" accept="audio/*" disabled />
        <button type="button" disabled>本地播放服务尚未接入</button>
        <button type="button" onClick={() => dispatch({ type: "playback_open", playback_id: `sim-${newId()}`, output_epoch: (state.playback?.output_epoch ?? 0) + 1, source: "simulated" })}>打开模拟播放</button>
        <button type="button" onClick={() => state.playback && dispatch({ type: "playback", feedback: { ...state.playback, phase: "buffering", amplitude: 0, source: "simulated" } })}>模拟buffering</button>
        <button type="button" onClick={() => state.playback && dispatch({ type: "playback", feedback: { ...state.playback, phase: "playing", amplitude: 0.5, source: "simulated" } })}>模拟playing</button>
        <button type="button" onClick={() => state.playback && dispatch({ type: "playback", feedback: { ...state.playback, phase: "drained", amplitude: 0, source: "simulated" } })}>模拟drained</button>
        <label htmlFor="avatar-size">实际展示尺寸</label>
        <input id="avatar-size" type="number" min="1" value={size ?? ""} onChange={(event) => {
          const value = Number(event.target.value);
          if (Number.isFinite(value) && value > 0) { sizeRef.current = value; setSize(value); setSizeSource("用户调整"); }
        }} />
        <label htmlFor="motion-limit">播放微动限幅</label>
        <input id="motion-limit" type="number" min="0" value={motionLimit} onChange={(event) => {
          const value = Math.max(0, Number(event.target.value) || 0);
          motionLimitRef.current = value;
          setMotionLimit(value);
        }} />
        <p>尺寸 {size === null ? "等待资源加载" : `${size}px`}（来源：{sizeSource}）；资源快照 647e9bd7；微动限幅 {motionLimit}px（零表示未调优）。</p>
        <label htmlFor="effect-duration">效果时长（毫秒）</label>
        <input id="effect-duration" value={effectDuration} onChange={(event) => setEffectDuration(event.target.value)} />
        <label htmlFor="effect-gap">效果最小间隔（毫秒）</label>
        <input id="effect-gap" value={effectGap} onChange={(event) => {
          const value = event.target.value;
          effectGapRef.current = value;
          setEffectGap(value);
          const parsed = Number(value);
          if (value && Number.isFinite(parsed) && parsed >= 0) effectsRef.current?.setGap(parsed);
        }} />
        <button type="button" onClick={enqueueManualEffect}>排队弹跳效果</button>
        <label><input type="checkbox" checked={state.reduced_motion} onChange={(event) => toggleReducedMotion(event.target.checked)} />减少动态</label>
        <button type="button" onClick={resetVisual}>重置视觉（保留业务事实）</button>
        <button type="button" onClick={rebuildAvatar}>重建角色（保留输入与资格）</button>
        <label htmlFor="effect-id">效果ID</label>
        <input id="effect-id" value={effectId} onChange={(event) => setEffectId(event.target.value)} />
        <button type="button" onClick={() => { if (effectId) { effectsRef.current?.stop(effectId); dispatch({ type: "control", action: "stop_effect", effect_id: effectId }); } }}>停止指定效果</button>
        <p data-testid="visual-effects">活动效果：{effectSnapshot.active ?? "无"}；队列：{effectSnapshot.queued.join(", ") || "空"}；当前最小间隔 {effectSnapshot.min_gap_ms}ms</p>
        <button type="button" onClick={destroyPreview}>销毁预览</button>
      </fieldset>

      <button type="button" onClick={reopen}>显式重开预览会话</button>

      <section aria-labelledby="diagnostics-title">
        <h2 id="diagnostics-title">诊断与证据</h2>
        <label><input type="checkbox" checked={recordComplete} onChange={(event) => {
          recordCompleteRef.current = event.target.checked;
          setRecordComplete(event.target.checked);
          setRecordingMode(event.target.checked ? "完整录制：最多10000条" : "抽样诊断：最近250条");
        }} />完整验收录制（上限10000条）</label>
        <p>{recordingMode}；抽样淘汰 {dropped} 条。</p>
        <button type="button" onClick={exportEvidence}>导出来源轨迹</button>
        <output data-testid="diagnostics">{diagnostics.map((entry) => {
          const payload = entry.event?.payload;
          const detail = payload?.type === "emotion" ? `；${payload.status}${payload.label ? ` ${payload.label}` : ""}` : "";
          const envelope = entry.event ? ` [${entry.event.event_id} ${entry.event.stream_id}#${entry.event.producer_seq}]` : "";
          return `${entry.source}：${entry.accepted ? "已投影" : "拒绝"}${detail}${envelope} — ${entry.reason}`;
        }).join("\n") || "暂无诊断"}</output>
        <pre>{JSON.stringify({ projection: state, sampled_diagnostics: diagnostics, dropped }, null, 2)}</pre>
      </section>
      <style>{`
        .avatar-preview { max-width: 1120px; margin: 0 auto; padding: 20px; color: #202634; line-height: 1.55; }
        .avatar-preview h1 { margin: 0 0 8px; font-size: 1.8rem; }
        .avatar-preview h2 { margin: 20px 0 10px; font-size: 1.2rem; }
        .avatar-preview fieldset, .avatar-preview section { min-width: 0; margin: 16px 0; padding: 16px; border: 1px solid #aab4c4; border-radius: 10px; background: #fff; }
        .avatar-preview legend { padding: 0 8px; font-weight: 650; }
        .avatar-preview label { display: block; margin: 10px 0 4px; font-weight: 600; }
        .avatar-preview button, .avatar-preview select, .avatar-preview input, .avatar-preview textarea { box-sizing: border-box; min-height: 38px; margin: 4px 6px 4px 0; padding: 7px 10px; border: 1px solid #8996a8; border-radius: 6px; background: #fff; color: inherit; font: inherit; }
        .avatar-preview button { cursor: pointer; }
        .avatar-preview button:hover:not(:disabled) { background: #eef4ff; border-color: #4169a1; }
        .avatar-preview button:disabled { cursor: not-allowed; opacity: .58; }
        .avatar-preview textarea { display: block; width: min(100%, 720px); min-height: 86px; }
        .avatar-preview input[type="number"], .avatar-preview input:not([type]), .avatar-preview select { max-width: min(100%, 420px); }
        .avatar-preview .emotion-buttons { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin: 10px 0 16px; }
        .avatar-preview .emotion-buttons button { display: flex; flex-direction: column; align-items: flex-start; min-height: 64px; margin: 0; text-align: left; }
        .avatar-preview .emotion-buttons small { color: #4d596b; font-size: .82rem; }
        .avatar-preview [data-testid="active-tasks"] { padding-left: 24px; }
        .avatar-preview [data-testid="diagnostics"] { display: block; max-height: 240px; overflow: auto; white-space: pre-wrap; padding: 10px; border: 1px solid #c5ced9; border-radius: 6px; }
        .avatar-preview pre { max-height: 320px; overflow: auto; padding: 12px; background: #f3f5f8; border-radius: 8px; font-size: .8rem; }
        @media (max-width: 640px) { .avatar-preview { padding: 12px; } .avatar-preview fieldset, .avatar-preview section { padding: 12px; } .avatar-preview .emotion-buttons { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; } .avatar-preview .emotion-buttons button { min-height: 76px; } }
      `}</style>
    </main>
  );
}
