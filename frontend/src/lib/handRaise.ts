import { useCallback, useEffect, useState } from "react";
import type { LocalParticipant, Participant } from "livekit-client";
import { ParticipantEvent } from "livekit-client";
import { useLocalParticipant } from "@livekit/components-react";

/**
 * Hand-raise state lives on `participant.metadata` (a free-form JSON string
 * each participant can update for themselves when granted
 * `can_update_own_metadata`, which the app does by default). We merge the
 * `handRaised`/`handRaisedAt` keys with whatever else may live there
 * (e.g. `auto_mute` written by the backend at token mint time) instead of
 * overwriting the whole blob.
 *
 * The owner can clear another participant's flag via the server-side
 * `update_participant` admin API; see `POST /v1/meetings/{id}/lower-hand`
 * in `meeting-api/app/routes/moderation.py`.
 */

export interface HandRaiseState {
  raised: boolean;
  raisedAt: number | null;
}

function parseMetadata(raw: string | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function readHand(raw: string | undefined): HandRaiseState {
  const obj = parseMetadata(raw);
  return {
    raised: obj.handRaised === true,
    raisedAt: typeof obj.handRaisedAt === "number" ? (obj.handRaisedAt as number) : null,
  };
}

export function useHandRaiseState(participant: Participant | undefined | null): HandRaiseState {
  const [state, setState] = useState<HandRaiseState>(() => readHand(participant?.metadata));
  useEffect(() => {
    if (!participant) {
      setState({ raised: false, raisedAt: null });
      return;
    }
    const update = () => setState(readHand(participant.metadata));
    update();
    participant.on(ParticipantEvent.ParticipantMetadataChanged, update);
    return () => {
      participant.off(ParticipantEvent.ParticipantMetadataChanged, update);
    };
  }, [participant]);
  return state;
}

async function writeMetadata(p: LocalParticipant, patch: Record<string, unknown>) {
  const current = parseMetadata(p.metadata);
  const next = { ...current, ...patch };
  // Strip explicit `undefined` so a `{ handRaised: undefined }` patch removes
  // the key instead of serialising as `null`.
  for (const k of Object.keys(next)) {
    if (next[k] === undefined) delete next[k];
  }
  await p.setMetadata(JSON.stringify(next));
}

export function useToggleHandRaise() {
  const { localParticipant } = useLocalParticipant();
  const state = useHandRaiseState(localParticipant);

  const raise = useCallback(async () => {
    if (!localParticipant) return;
    await writeMetadata(localParticipant, {
      handRaised: true,
      handRaisedAt: Date.now(),
    });
  }, [localParticipant]);

  const lower = useCallback(async () => {
    if (!localParticipant) return;
    await writeMetadata(localParticipant, {
      handRaised: undefined,
      handRaisedAt: undefined,
    });
  }, [localParticipant]);

  const toggle = useCallback(async () => {
    if (state.raised) await lower();
    else await raise();
  }, [state.raised, raise, lower]);

  return { ...state, raise, lower, toggle };
}
