/**
 * Clean a PlaybackItem filename into a display title — strip a leading
 * `NN_` numeric prefix (possibly repeated), replace underscores with
 * spaces, and drop the file extension. Mirrors `whats_next_slide.clean_title`
 * (Python) so every surface that shows a playlist filename — the in-meeting
 * Video-playback panel, the On Demand list, the "What's up next" slide —
 * renders the same name.
 *
 * Examples:
 *   "02_The_Silent_Massacre_Research_Report.mp4" → "The Silent Massacre Research Report"
 *   "Cognitive Liberty Dystopia.mp4"             → "Cognitive Liberty Dystopia"
 */
export function cleanPlaylistTitle(filename: string): string {
  let name = filename.replace(/\.[^./\\]+$/, "");
  while (/^\d+_/.test(name)) {
    name = name.replace(/^\d+_/, "");
  }
  const cleaned = name.replace(/_/g, " ").trim();
  return cleaned || filename;
}
