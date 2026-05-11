import { bootstrapFromOneWitysk, clearAccessToken, getAccessToken } from "./auth";

export interface AnonTokenResponse {
  livekit_url: string;
  token: string;
  room_name: string;
  ice_servers: {
    username: string;
    credential: string;
    urls: string[];
    ttl: number;
  } | null;
}

export interface MeetingOut {
  id: string;
  room_name: string;
  display_title: string;
  owner_user_id: string;
  owner_name?: string | null;
  is_active: boolean;
  require_password: boolean;
  max_participants: number;
  branding_url?: string | null;
  list_for_authenticated?: boolean;
  list_for_anonymous?: boolean;
}

export interface PublicMeeting {
  room_name: string;
  display_title: string;
  max_participants: number;
  require_password: boolean;
  branding_url: string | null;
  owner_name: string | null;
}

export interface PublicRoomInfo {
  room_name: string;
  display_title: string;
  require_password: boolean;
  branding_url: string | null;
  owner_name: string | null;
}

export interface UserPreferencesOut {
  language: string | null;
  language_set_manually: boolean;
  anonymise_email_in_join_log?: boolean;
  dont_log_my_ip?: boolean;
}

export interface MeOut {
  id: number;
  kind: "sso" | "native";
  external_id: string | null;
  email: string | null;
  username: string | null;
  name: string | null;
  facepic_path: string | null;
  is_admin: boolean;
  is_voucher_admin: boolean;
  is_platform_admin: boolean;
  trial_used: boolean;
  trial_days_remaining: number | null;
  entitlement_kind: string | null;
  entitlement_expires_at: string | null;
  totp_enabled: boolean;
  totp_recovery_remaining: number;
  email_otp_enabled: boolean;
}

// ─── Admin panel ──────────────────────────────────────────────────────

export interface AdminUserOut {
  id: number;
  kind: "sso" | "native";
  external_id: string | null;
  email: string | null;
  username: string | null;
  name: string | null;
  is_admin: boolean;
  is_platform_admin: boolean;
  is_disabled: boolean;
  disable_reason: string | null;
  trial_used: boolean;
  entitlement_kind: string | null;
  entitlement_expires_at: string | null;
  totp_enabled: boolean;
  email_otp_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminUserList {
  total: number;
  users: AdminUserOut[];
}

export interface BlockedIPOut {
  id: number;
  ip_address: string;
  reason: string | null;
  blocked_by_user_id: number | null;
  block_count: number;
  is_enabled: boolean;
  created_at: string;
  live_hits: number;
}

export interface IdsTempBlock {
  ip: string;
  expires_at: string;
  seconds_remaining: number;
  hits: number;
}

export interface IdsStatusOut {
  enabled: boolean;
  tracked_ips: number;
  temp_blocked: number;
  events_in_memory: number;
  temp_blocks: IdsTempBlock[];
}

export interface IdsEvent {
  ts: string | null;
  event_type: string;
  severity: string;
  ip: string | null;
  user_id: number | null;
  handle: string | null;
  path: string | null;
  user_agent: string | null;
  details: string | null;
}

export type LoginResult =
  | { kind: "ok"; access_token: string; user: MeOut }
  | {
      kind: "2fa";
      challenge_token: string;
      totp_enabled: boolean;
      email_otp_enabled: boolean;
      // Set when the backend already auto-mailed a fresh code on login (i.e.
      // the user's only second factor is email). Masked address for display.
      email_otp_sent_to: string | null;
    };

export interface ChatReactionDTO {
  emoji: string;
  reactor_identity: string;
  reactor_name: string;
}

export interface ChatMessageDTO {
  id: number;
  sender_identity: string;
  sender_name: string;
  message: string;
  reply_to_id: number | null;
  sent_at: string;
  attachment: {
    url: string;
    type: string | null;
    name: string | null;
    size: number | null;
  } | null;
  reactions: ChatReactionDTO[];
}

export const CHAT_REACTIONS = ["😊", "👍", "😂", "😢", "😠", "🤓", "❤️", "👎"] as const;
export type ChatReactionEmoji = (typeof CHAT_REACTIONS)[number];

export interface BillingHistoryItem {
  date: string;
  kind:
    | "paypal_order_monthly"
    | "paypal_order_annual"
    | "paypal_subscription_monthly"
    | "paypal_subscription_annual"
    | "voucher";
  label: string;
  amount: string | null;
  currency: string | null;
  status: string | null;
  reference: string | null;
}

export interface VoucherOut {
  id: number;
  code: string;
  duration_days: number;
  note: string | null;
  issued_by: string;
  redeemed_by_user_id: number | null;
  redeemed_at: string | null;
  created_at: string;
  expires_at: string;
}

async function fetchOnce(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(path, { ...init, headers });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await fetchOnce(path, init, getAccessToken());

  // If the JWT expired, our cached token is stale. one.witysk.org's SPA
  // auto-refreshes its own localStorage token (TokenManager), so a fresh
  // re-bootstrap usually picks up a valid one. Try exactly once, then bail.
  if (res.status === 401) {
    let detail = "";
    try {
      detail = (await res.clone().json())?.detail ?? "";
    } catch {
      /* ignore */
    }
    if (/expired|invalid token/i.test(detail)) {
      clearAccessToken();
      const fresh = await bootstrapFromOneWitysk();
      if (fresh) {
        res = await fetchOnce(path, init, fresh);
      }
    }
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  createMeeting: (body: {
    display_title: string;
    password?: string;
    list_for_authenticated?: boolean;
    list_for_anonymous?: boolean;
    display_name?: string | null;
    auto_admit_authenticated?: boolean;
    require_name_on_join?: boolean;
    auto_mute_new_joiners?: boolean;
    auto_disable_camera_for_new?: boolean;
    waiting_room_enabled?: boolean;
    lock_room_after_start?: boolean;
    allow_participant_screenshare?: boolean;
    allow_participant_chat?: boolean;
  }) =>
    request<{ meeting: MeetingOut; join_url: string }>("/api/v1/meetings", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateMeeting: (
    meetingId: string,
    body: {
      display_title?: string;
      list_for_authenticated?: boolean;
      list_for_anonymous?: boolean;
      recording_mode?: "manual" | "auto_on_start" | "off";
    }
  ) =>
    request<MeetingOut>(`/api/v1/meetings/${meetingId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  listMeetings: () => request<MeetingOut[]>("/api/v1/meetings"),

  listDiscoverable: () => request<PublicMeeting[]>("/api/v1/discoverable"),

  listPublicMeetings: () => request<PublicMeeting[]>("/api/v1/public-meetings"),

  ownerToken: (meetingId: string, body: { display_name?: string | null } = {}) =>
    request<AnonTokenResponse>(`/api/v1/meetings/${meetingId}/token`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  anonToken: (roomName: string, body: { display_name: string; email?: string; password?: string }) =>
    request<AnonTokenResponse>(`/api/v1/rooms/${roomName}/anon-token`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  mute: (meetingId: string, body: { participant_identity: string; mute: boolean }) =>
    request(`/api/v1/meetings/${meetingId}/mute`, { method: "POST", body: JSON.stringify(body) }),

  muteAll: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}/mute-all`, { method: "POST" }),

  kick: (meetingId: string, participant_identity: string) =>
    request(`/api/v1/meetings/${meetingId}/kick`, {
      method: "POST",
      body: JSON.stringify({ participant_identity }),
    }),

  endMeeting: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}`, { method: "DELETE" }),

  deleteMeeting: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}`, { method: "DELETE" }),

  reopenMeeting: (meetingId: string) =>
    request<MeetingOut>(`/api/v1/meetings/${meetingId}/reopen`, { method: "POST" }),

  // Café — always-on audio room. The token bears `room: "ti-cafe"`; LiveKit
  // auto-creates the room on first join.
  tiCafeToken: () =>
    request<AnonTokenResponse>("/api/v1/ti-cafe/token", { method: "POST" }),

  tiCafeLive: () =>
    request<{ user_ids: number[] }>("/api/v1/ti-cafe/live"),

  setPresenter: (meetingId: string, participant_identity: string | null) =>
    request(`/api/v1/meetings/${meetingId}/presenter`, {
      method: "POST",
      body: JSON.stringify({ participant_identity }),
    }),

  // ─── Native auth ─────────────────────────────────────────────────────
  signup: (body: { email: string; username: string; password: string; name?: string | null }) =>
    request<{ access_token: string; user: MeOut }>("/api/v1/auth/signup", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Login may return either a normal access token + user, or, when the
   * account has 2FA enabled, a short-lived challenge token that must be
   * exchanged via `loginVerify2fa` together with a TOTP code. The wrapper
   * normalises both shapes into a discriminated union. */
  async login(body: { handle: string; password: string }): Promise<LoginResult> {
    const r = await request<
      | { access_token: string; user: MeOut }
      | {
          requires_2fa: true;
          challenge_token: string;
          totp_enabled: boolean;
          email_otp_enabled: boolean;
          email_otp_sent_to: string | null;
        }
    >("/api/v1/auth/login", { method: "POST", body: JSON.stringify(body) });
    if ("requires_2fa" in r && r.requires_2fa) {
      return {
        kind: "2fa",
        challenge_token: r.challenge_token,
        totp_enabled: r.totp_enabled,
        email_otp_enabled: r.email_otp_enabled,
        email_otp_sent_to: r.email_otp_sent_to,
      };
    }
    const ok = r as { access_token: string; user: MeOut };
    return { kind: "ok", access_token: ok.access_token, user: ok.user };
  },

  loginVerify2fa: (body: { challenge_token: string; code: string }) =>
    request<{ access_token: string; user: MeOut }>("/api/v1/auth/login/2fa", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  loginSendEmailOtp: (challenge_token: string) =>
    request<{ ok: boolean; sent_to: string }>("/api/v1/auth/login/email-otp/send", {
      method: "POST",
      body: JSON.stringify({ challenge_token }),
    }),

  logout: () => request<{ ok: boolean }>("/api/v1/auth/logout", { method: "POST" }),

  me: () => request<MeOut>("/api/v1/me"),

  updateMe: (body: { name?: string | null; email?: string | null; username?: string | null }) =>
    request<MeOut>("/api/v1/me", { method: "PATCH", body: JSON.stringify(body) }),

  changePassword: (body: { current_password: string; new_password: string }) =>
    request<{ ok: boolean }>("/api/v1/me/password", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Native-user facepic upload — multipart, image-only, 5 MB cap server-side. */
  async uploadFacepic(file: File): Promise<MeOut> {
    const tok = getAccessToken();
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/v1/me/facepic", {
      method: "POST",
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
      body: fd,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.clone().json();
        detail = j.detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new Error(detail);
    }
    return (await res.json()) as MeOut;
  },

  deleteFacepic: () => request<MeOut>("/api/v1/me/facepic", { method: "DELETE" }),

  // ─── Vouchers ────────────────────────────────────────────────────────
  issueVoucher: (body: { duration_days: number; note?: string | null }) =>
    request<VoucherOut>("/api/v1/vouchers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listVouchers: () => request<VoucherOut[]>("/api/v1/vouchers"),

  revokeVoucher: (code: string) =>
    request<{ ok: boolean; revoked_user_id: number | null }>(
      `/api/v1/vouchers/${encodeURIComponent(code)}`,
      { method: "DELETE" }
    ),

  redeemVoucher: (code: string) =>
    request<{ ok: boolean; duration_days: number; entitlement_expires_at: string }>(
      "/api/v1/vouchers/redeem",
      { method: "POST", body: JSON.stringify({ code }) }
    ),

  // ─── Password reset + account deletion ──────────────────────────────
  requestPasswordReset: (email: string) =>
    request<{ ok: boolean }>("/api/v1/auth/password-reset/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),

  confirmPasswordReset: (token: string, new_password: string) =>
    request<{ ok: boolean }>("/api/v1/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({ token, new_password }),
    }),

  deleteMyAccount: (password: string) =>
    request<{ ok: boolean }>("/api/v1/me", {
      method: "DELETE",
      body: JSON.stringify({ password }),
    }),

  startTrial: () => request<MeOut>("/api/v1/me/start-trial", { method: "POST" }),

  // ─── 2FA (TOTP) ─────────────────────────────────────────────────────
  totpSetup: () =>
    request<{ secret: string; otpauth_uri: string }>("/api/v1/me/2fa/setup", { method: "POST" }),

  totpEnable: (code: string) =>
    request<{ ok: boolean; recovery_codes: string[] }>("/api/v1/me/2fa/enable", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  totpDisable: (body: { password: string; code: string }) =>
    request<{ ok: boolean }>("/api/v1/me/2fa/disable", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  totpRegenerateRecovery: (code: string) =>
    request<{ ok: boolean; recovery_codes: string[] }>("/api/v1/me/2fa/recovery-regenerate", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  emailOtpStart: () =>
    request<{ ok: boolean; sent_to: string }>("/api/v1/me/2fa/email/start", { method: "POST" }),

  emailOtpConfirm: (code: string) =>
    request<{ ok: boolean }>("/api/v1/me/2fa/email/confirm", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  emailOtpDisable: (password: string) =>
    request<{ ok: boolean }>("/api/v1/me/2fa/email/disable", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),

  // ─── PayPal billing ─────────────────────────────────────────────────
  billingConfig: () =>
    request<{
      enabled: boolean;
      client_id: string;
      plan_id_monthly: string;
      plan_id_annual: string;
      monthly_price: string;
      monthly_currency: string;
      annual_price: string;
      annual_currency: string;
    }>("/api/v1/billing/config"),

  createPaypalOrder: (kind: "monthly" | "annual" = "annual") =>
    request<{ order_id: string; kind: string }>("/api/v1/billing/orders", {
      method: "POST",
      body: JSON.stringify({ kind }),
    }),

  capturePaypalOrder: (orderId: string) =>
    request<{ ok: boolean; status?: string; already_captured?: boolean; kind?: string }>(
      `/api/v1/billing/orders/${encodeURIComponent(orderId)}/capture`,
      { method: "POST" }
    ),

  activatePaypalSubscription: (subscriptionId: string, plan: "monthly" | "annual" = "monthly") =>
    request<{ ok: boolean; status: string; plan: string }>("/api/v1/billing/subscriptions/activated", {
      method: "POST",
      body: JSON.stringify({ subscription_id: subscriptionId, plan }),
    }),

  cancelPaypalSubscription: () =>
    request<{ ok: boolean }>("/api/v1/billing/subscriptions/cancel", { method: "POST" }),

  myBillingHistory: () =>
    request<BillingHistoryItem[]>("/api/v1/billing/me/billing-history"),

  listChat: (roomName: string) =>
    request<ChatMessageDTO[]>(`/api/v1/rooms/${roomName}/chat`),

  /** Owner-only — fetch a closed (or active) meeting's chat by id, for the
   * post-meeting transcript view from MyMeetings. */
  listMeetingChat: (meetingId: string) =>
    request<ChatMessageDTO[]>(`/api/v1/meetings/${meetingId}/chat`),

  postChat: (
    roomName: string,
    body: {
      sender_identity: string;
      sender_name: string;
      message: string;
      reply_to_id?: number | null;
    }
  ) =>
    request<ChatMessageDTO>(`/api/v1/rooms/${roomName}/chat`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Multipart attachment upload — image-only, 5 MB cap server-side. */
  async postChatAttachment(
    roomName: string,
    fields: {
      sender_identity: string;
      sender_name: string;
      message?: string;
      reply_to_id?: number | null;
      file: File;
    }
  ): Promise<ChatMessageDTO> {
    const fd = new FormData();
    fd.append("sender_identity", fields.sender_identity);
    fd.append("sender_name", fields.sender_name);
    if (fields.message) fd.append("message", fields.message);
    if (fields.reply_to_id != null) fd.append("reply_to_id", String(fields.reply_to_id));
    fd.append("file", fields.file);
    const res = await fetch(`/api/v1/rooms/${roomName}/chat/attachment`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.clone().json();
        detail = j.detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new Error(detail);
    }
    return (await res.json()) as ChatMessageDTO;
  },

  putChatReaction: (
    roomName: string,
    messageId: number,
    body: { reactor_identity: string; reactor_name: string; emoji: string }
  ) =>
    request<{ ok: boolean }>(
      `/api/v1/rooms/${roomName}/chat/${messageId}/reaction`,
      { method: "PUT", body: JSON.stringify(body) }
    ),

  deleteChatReaction: (roomName: string, messageId: number, reactorIdentity: string) =>
    request<void>(
      `/api/v1/rooms/${roomName}/chat/${messageId}/reaction?reactor_identity=${encodeURIComponent(
        reactorIdentity
      )}`,
      { method: "DELETE" }
    ),

  startRecording: (
    meetingId: string,
    body: { layout?: "speaker" | "grid" | "single-speaker" } = {}
  ) =>
    request(`/api/v1/meetings/${meetingId}/recordings:start`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  stopRecording: (meetingId: string) =>
    request(`/api/v1/meetings/${meetingId}/recordings:stop`, { method: "POST" }),

  listRecordings: () => request<unknown[]>("/api/v1/recordings"),

  publishYoutube: (
    recordingId: string,
    body: { title?: string; description?: string; privacy?: "public" | "unlisted" | "private" } = {}
  ) =>
    request<{ ok: boolean; url: string; video_id: string }>(
      `/api/v1/recordings/${recordingId}/publish-youtube`,
      { method: "POST", body: JSON.stringify(body) }
    ),

  /**
   * Download a recording. The endpoint requires a Bearer token, which a
   * plain `<a href>` cannot carry — so we fetch the file with the
   * `Authorization` header, materialise a Blob URL, and trigger a save via
   * a synthesised anchor click. The server's `Content-Disposition` is
   * preserved as the suggested filename if present; otherwise `fallbackName`
   * is used.
   *
   * Caveat: the whole file ends up in browser memory. For typical meeting
   * lengths this is fine (~few hundred MB). For multi-GB files, switch to a
   * service-worker-streamed download or a short-lived signed URL.
   */
  async downloadRecording(recordingId: string, fallbackName: string): Promise<void> {
    const tok = getAccessToken();
    let res = await fetch(`/api/v1/recordings/${recordingId}/download`, {
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
    });
    if (res.status === 401) {
      // Cached token may be stale; clear it so the bootstrap re-fetches the
      // current value from one.witysk.org's localStorage.
      clearAccessToken();
      const fresh = await bootstrapFromOneWitysk();
      if (fresh) {
        res = await fetch(`/api/v1/recordings/${recordingId}/download`, {
          headers: { Authorization: `Bearer ${fresh}` },
        });
      }
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.clone().json();
        detail = data.detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new Error(detail);
    }

    // Pull a filename out of Content-Disposition if the server set one.
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd);
    const filename = m ? decodeURIComponent(m[1].replace(/"$/, "")) : fallbackName;

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Give the browser a tick to start the download before revoking.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },

  deleteRecording: (recordingId: string) =>
    request(`/api/v1/recordings/${recordingId}`, { method: "DELETE" }),

  invite: (
    meetingId: string,
    body: { emails: string[]; personal_message?: string; display_name?: string | null }
  ) =>
    request<{ ok: boolean; sent: number; failed: string[]; join_url: string }>(
      `/api/v1/meetings/${meetingId}/invite`,
      { method: "POST", body: JSON.stringify(body) }
    ),

  /** Public: meeting metadata for the lobby (no auth). */
  publicRoomInfo: (roomName: string) =>
    request<PublicRoomInfo>(`/api/v1/rooms/${roomName}/info`),

  getMyPreferences: () =>
    request<UserPreferencesOut>("/api/v1/me/preferences"),

  updateMyPreferences: (body: {
    language?: string;
    anonymise_email_in_join_log?: boolean;
    dont_log_my_ip?: boolean;
  }) =>
    request<UserPreferencesOut>("/api/v1/me/preferences", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // ─── Admin panel ─────────────────────────────────────────────────────
  adminListUsers: (
    params: {
      q?: string;
      kind?: "sso" | "native";
      limit?: number;
      offset?: number;
      sort_by?: "id" | "kind" | "email" | "username" | "name" | "created_at";
      sort_order?: "asc" | "desc";
    } = {}
  ) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.kind) qs.set("kind", params.kind);
    if (params.limit != null) qs.set("limit", String(params.limit));
    if (params.offset != null) qs.set("offset", String(params.offset));
    if (params.sort_by) qs.set("sort_by", params.sort_by);
    if (params.sort_order) qs.set("sort_order", params.sort_order);
    const tail = qs.toString();
    return request<AdminUserList>(`/api/v1/admin/users${tail ? `?${tail}` : ""}`);
  },

  adminGetUser: (userId: number) =>
    request<AdminUserOut>(`/api/v1/admin/users/${userId}`),

  adminUpdateUser: (
    userId: number,
    body: { is_platform_admin?: boolean; is_disabled?: boolean; disable_reason?: string | null }
  ) =>
    request<AdminUserOut>(`/api/v1/admin/users/${userId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  adminSetPassword: (userId: number, new_password: string) =>
    request<{ ok: boolean }>(`/api/v1/admin/users/${userId}/set-password`, {
      method: "POST",
      body: JSON.stringify({ new_password }),
    }),

  adminDeleteUser: (userId: number) =>
    request<{ ok: boolean }>(`/api/v1/admin/users/${userId}`, { method: "DELETE" }),

  adminListBlockedIps: () => request<BlockedIPOut[]>("/api/v1/admin/blocked-ips"),

  adminAddBlockedIp: (body: { ip_address: string; reason?: string | null }) =>
    request<BlockedIPOut>("/api/v1/admin/blocked-ips", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  adminUpdateBlockedIp: (id: number, body: { is_enabled?: boolean; reason?: string | null }) =>
    request<BlockedIPOut>(`/api/v1/admin/blocked-ips/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  adminDeleteBlockedIp: (id: number) =>
    request<{ ok: boolean }>(`/api/v1/admin/blocked-ips/${id}`, { method: "DELETE" }),

  adminIdsStatus: () => request<IdsStatusOut>("/api/v1/admin/ids/status"),

  adminIdsEvents: (params: { limit?: number; persisted?: boolean } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit != null) qs.set("limit", String(params.limit));
    if (params.persisted) qs.set("persisted", "true");
    const tail = qs.toString();
    return request<IdsEvent[]>(`/api/v1/admin/ids/events${tail ? `?${tail}` : ""}`);
  },

  adminIdsUnblock: (ip: string) =>
    request<{ ok: boolean; was_blocked: boolean }>(
      `/api/v1/admin/ids/unblock/${encodeURIComponent(ip)}`,
      { method: "POST" }
    ),

  /** Upload (or replace) a meeting's branding image. */
  async uploadBranding(meetingId: string, file: File): Promise<{ branding_url: string | null }> {
    const tok = getAccessToken();
    const fd = new FormData();
    fd.append("file", file);
    let res = await fetch(`/api/v1/meetings/${meetingId}/branding`, {
      method: "POST",
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
      body: fd,
    });
    if (res.status === 401) {
      clearAccessToken();
      const fresh = await bootstrapFromOneWitysk();
      if (fresh) {
        res = await fetch(`/api/v1/meetings/${meetingId}/branding`, {
          method: "POST",
          headers: { Authorization: `Bearer ${fresh}` },
          body: fd,
        });
      }
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.clone().json();
        detail = j.detail ?? detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return res.json();
  },
};
