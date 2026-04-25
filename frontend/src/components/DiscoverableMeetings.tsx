import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Compass, Globe, LogIn, Lock } from "lucide-react";
import { api, PublicMeeting } from "../lib/api";
import { isAuthenticated } from "../lib/auth";
import { Button, Card } from "./ui";

/**
 * Lists meetings owned by OTHER users that have opted into discoverability.
 *
 * - When the viewer is signed-in, we use the authenticated `/api/v1/discoverable`
 *   endpoint, which returns meetings flagged for either authenticated OR
 *   anonymous visibility.
 * - When the viewer is anonymous, we fall back to `/api/v1/public-meetings`
 *   which returns only those flagged for anonymous visibility.
 *
 * Renders nothing if the result is empty (no clutter on solo/private setups).
 */
export default function DiscoverableMeetings() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [rows, setRows] = useState<PublicMeeting[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const auth = isAuthenticated();
    (auth ? api.listDiscoverable() : api.listPublicMeetings())
      .then((r) => !cancelled && setRows(r))
      .catch((e) => !cancelled && setErr((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, []);

  if (err) {
    // Quiet failure — discover isn't critical to the page.
    return null;
  }
  if (!rows || rows.length === 0) {
    return null;
  }

  return (
    <Card data-testid="discoverable-meetings">
      <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
        <Compass size={18} className="text-accent-500" />
        {t("discover.title")}
        <span className="text-sm font-normal text-slate-400">{t("discover.subtitle")}</span>
      </h2>
      <ul className="flex flex-col divide-y divide-primary-700">
        {rows.map((m) => (
          <li
            key={m.room_name}
            data-testid={`discover-row-${m.room_name}`}
            className="py-3 flex items-center gap-3 first:pt-0 last:pb-0"
          >
            {m.branding_url && (
              <img
                src={m.branding_url}
                alt=""
                className="h-10 w-10 object-cover rounded-md border border-primary-700 flex-shrink-0"
              />
            )}
            <div className="flex-1 min-w-0">
              <div className="font-medium text-slate-50 truncate flex items-center gap-2">
                {m.display_title}
                {m.require_password && <Lock size={14} className="text-slate-400" />}
                <Globe size={14} className="text-accent-500" />
              </div>
              <div className="text-xs text-slate-400 mt-0.5">
                <code>{m.room_name}</code> · {t("discover.maxParticipants", { n: m.max_participants })}
                {m.owner_name && (
                  <span className="ml-2" data-testid={`discover-host-${m.room_name}`}>
                    · {t("discover.hostedBy", { name: m.owner_name, defaultValue: "Hosted by {{name}}" })}
                  </span>
                )}
              </div>
            </div>
            <Button
              type="button"
              variant="accent"
              size="sm"
              onClick={() => navigate(`/${m.room_name}`)}
              data-testid={`discover-join-${m.room_name}`}
            >
              <LogIn size={16} /> {t("discover.join")}
            </Button>
          </li>
        ))}
      </ul>
    </Card>
  );
}
