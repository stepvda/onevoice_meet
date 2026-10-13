import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { embed, useEmbed } from "../lib/embed";

/**
 * Router side of the Mail embed protocol (lib/embed.ts): posts
 * `meet:location` on every route change and shows the paths Mail sends with
 * `ovm:navigate`. Renders nothing; inert when Meet is not embedded.
 */
export default function EmbedBridge() {
  const { status } = useEmbed();
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (status !== "on") return;
    return embed().onNavigate((path) => navigate(path));
  }, [status, navigate]);

  useEffect(() => {
    if (status !== "on") return;
    embed().postLocation(`${location.pathname}${location.search}`);
  }, [status, location.pathname, location.search]);

  return null;
}
