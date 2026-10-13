import { describe, expect, it } from "vitest";
import {
  applyLocalPlaybackFeedback,
  applyVoiceBehaviorEvent,
  initialVoiceAvatarState,
  projectVoiceAvatar,
  replayVoiceBehaviorEvents,
} from "./avatar-projection";
import type { VoiceBehaviorEvent } from "./avatar-projection";

const identity = {
  session_id: "session-1",
  input_turn_id: "input-1",
  input_order: 1,
  stimulus_revision: 0,
};

function event(
  seq: number,
  event_type: string,
  payload: Record<string, unknown>,
): VoiceBehaviorEvent {
  return {
    event_id: `event-${seq}`,
    schema_version: 1,
    event_type,
    stream_id: "voice-realtime-session",
    seq,
    session_id: identity.session_id,
    producer_id: "hexis.voice-realtime",
    producer_seq: seq + 100,
    source: "server",
    channel: "voice",
    payload,
    media_session_id: "media-1",
  };
}

function committed(seq = 1, inputIdentity = identity) {
  return event(seq, "voice_input_committed", { identity: inputIdentity });
}

describe("voice avatar production event projection", () => {
  it("replays A events by database sequence rather than producer sequence", () => {
    const events = [
      committed(1),
      event(2, "voice_visual_window", {
        identity,
        wordset_version: "v1",
        fact: { status: "open", owner: "service", decision_seq: 0 },
      }),
      event(3, "voice_visual_window", {
        identity,
        wordset_version: "v1",
        fact: { status: "accepted", owner: "service", decision_seq: 1 },
        label: "curious",
      }),
      event(4, "voice_model_call_started", {
        identity,
        activity_id: "response-1",
        state: "started",
      }),
      event(5, "voice_recall_started", {
        identity,
        activity_id: "call-1",
        state: "started",
      }),
    ];
    const initialState = initialVoiceAvatarState(identity.session_id, "media-1");
    const live = events.reduce(applyVoiceBehaviorEvent, initialState);
    const replayed = replayVoiceBehaviorEvents(initialState, [...events].reverse());

    expect(projectVoiceAvatar(replayed)).toEqual(projectVoiceAvatar(live));
    expect(projectVoiceAvatar(replayed)).toMatchObject({
      bubble: "recalling",
      composition: { eye: "curious", pose: "thinking" },
    });
    expect(replayed.preview.seq["voice-realtime-session"]).toBe(3);
  });

  it("accepts only the current complete identity and projects recall over thinking", () => {
    let state = initialVoiceAvatarState(identity.session_id, "media-1");
    state = applyVoiceBehaviorEvent(state, committed());
    state = applyVoiceBehaviorEvent(state, event(2, "voice_visual_window", {
      identity,
      wordset_version: "v1",
      fact: { status: "open", owner: "service", decision_seq: 0 },
    }));
    state = applyVoiceBehaviorEvent(state, event(3, "voice_visual_window", {
      identity,
      wordset_version: "v1",
      fact: { status: "accepted", owner: "service", decision_seq: 1 },
      label: "happy",
    }));
    state = applyVoiceBehaviorEvent(state, event(4, "voice_model_call_started", {
      identity,
      activity_id: "response-1",
      response_id: "response-1",
      state: "started",
    }));
    state = applyVoiceBehaviorEvent(state, event(5, "voice_recall_started", {
      identity,
      activity_id: "call-1",
      call_id: "call-1",
      state: "started",
    }));

    expect(projectVoiceAvatar(state)).toMatchObject({
      bubble: "recalling",
      emotionLabel: "happy",
      composition: { eye: "happy", pose: "thinking" },
    });

    state = applyVoiceBehaviorEvent(state, event(6, "voice_recall_ended", {
      identity,
      activity_id: "call-1",
      state: "ended",
    }));
    expect(projectVoiceAvatar(state).bubble).toBe("thinking");
  });

  it("ignores old input and legacy waiting events without changing the current bubble", () => {
    let state = initialVoiceAvatarState(identity.session_id, "media-1");
    state = applyVoiceBehaviorEvent(state, committed());
    state = applyVoiceBehaviorEvent(state, event(2, "voice_visual_window", {
      identity,
      wordset_version: "v1",
      fact: { status: "open", owner: "service", decision_seq: 0 },
    }));
    state = applyVoiceBehaviorEvent(state, event(3, "voice_visual_window", {
      identity,
      wordset_version: "v1",
      fact: { status: "accepted", owner: "service", decision_seq: 1 },
      label: "happy",
    }));
    state = applyVoiceBehaviorEvent(state, event(4, "voice_model_call_started", {
      identity,
      activity_id: "response-1",
      state: "started",
    }));

    const nextIdentity = { ...identity, input_turn_id: "input-2", input_order: 2 };
    state = applyVoiceBehaviorEvent(state, committed(5, nextIdentity));
    expect(projectVoiceAvatar(state).emotionLabel).toBeUndefined();
    state = applyVoiceBehaviorEvent(state, event(6, "voice_recall_started", {
      identity,
      activity_id: "old-recall",
      state: "started",
    }));
    state = applyVoiceBehaviorEvent(state, event(7, "voice_model_call_started", {
      identity: nextIdentity,
      activity_id: "response-2",
      state: "started",
    }));
    state = applyVoiceBehaviorEvent(state, event(8, "waiting_for_user", {
      identity: nextIdentity,
      state: "started",
    }));

    expect(projectVoiceAvatar(state).bubble).toBe("thinking");
    expect(projectVoiceAvatar(state).composition.eye).toBe("happy");
  });

  it("保留历史回执身份，但不再用播放反馈驱动前台播报姿态", () => {
    const state = initialVoiceAvatarState(identity.session_id, "media-1");
    const playing = applyLocalPlaybackFeedback(state, {
      identity: {
        playback_id: "play-1",
        output_epoch: 1,
        response_id: "response-1",
        media_session_id: "media-1",
      },
      feedback: {
        playback_id: "play-1",
        output_epoch: 1,
        phase: "playing",
        amplitude: 0.31,
        progress: 0.1,
        played_ms: 100,
        interval_ms: 33,
        source: "real",
      },
    });
    const stale = applyLocalPlaybackFeedback(playing, {
      identity: {
        playback_id: "old-play",
        output_epoch: 0,
        response_id: "old-response",
        media_session_id: "old-media",
      },
      feedback: { ...playing.localPlayback!.feedback, amplitude: 0.9 },
    });

    expect(projectVoiceAvatar(playing).composition).toMatchObject({
      playing: false,
      amplitude: 0,
    });
    expect(stale.localPlayback?.identity.playback_id).toBe("play-1");
  });
});
