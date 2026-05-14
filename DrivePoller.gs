/**
 * apps-script/DrivePoller.gs
 * ──────────────────────────
 * OPTIONAL — only needed if you want to pre-process or filter files
 * before they reach the server, or if you prefer Apps Script to own
 * the Drive polling instead of the Python server.
 *
 * By default the Python server polls Drive directly.
 * This script is provided as an alternative / complement.
 *
 * Setup:
 *   1. Open https://script.google.com and create a new project.
 *   2. Paste this file in.
 *   3. Fill in CONFIG below.
 *   4. Run setupTrigger() once to register the time-based trigger.
 *   5. Authorise the script when prompted (needs Drive read access).
 *
 * The script will then run every POLL_INTERVAL_MINUTES and force the
 * server to re-fetch immediately by hitting /health (a no-op) or you
 * can extend it to POST directly to a webhook if you add one.
 */

// ── CONFIG — edit these values ────────────────────────────────────────────────

const CONFIG = {
  // Same folder ID as in your .env
  FOLDER_ID: "PASTE_YOUR_FOLDER_ID_HERE",              // ← EDIT

  // Public URL of your running server (same as SERVER_BASE_URL in .env)
  SERVER_URL: "http://YOUR_SERVER_IP_OR_DOMAIN:5000",  // ← EDIT

  // How often this script runs (minutes)
  // Should match or be shorter than REFRESH_SECONDS / 60 in your .env
  POLL_INTERVAL_MINUTES: 30,                           // ← EDIT if desired
};

// ── Trigger setup — run this function once manually ───────────────────────────

function setupTrigger() {
  // Remove existing triggers for this function to avoid duplicates
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === "pollDriveAndNotify")
    .forEach(t => ScriptApp.deleteTrigger(t));

  ScriptApp.newTrigger("pollDriveAndNotify")
    .timeBased()
    .everyMinutes(CONFIG.POLL_INTERVAL_MINUTES)
    .create();

  Logger.log("Trigger created — running every %d minutes.", CONFIG.POLL_INTERVAL_MINUTES);
}

// ── Main polling function ─────────────────────────────────────────────────────

function pollDriveAndNotify() {
  const folder = DriveApp.getFolderById(CONFIG.FOLDER_ID);
  const files  = folder.getFiles();

  const images = [];
  while (files.hasNext()) {
    const f = files.next();
    if (f.getMimeType().startsWith("image/")) {
      images.push({ name: f.getName(), modified: f.getLastUpdated() });
    }
  }

  if (images.length === 0) {
    Logger.log("No images in folder — nothing to do.");
    return;
  }

  // Sort newest first
  images.sort((a, b) => b.modified - a.modified);
  Logger.log("Found %d image(s). Newest: %s", images.length, images[0].name);

  // Ping the server health endpoint so you can see activity in server logs.
  // Extend this to POST to a /refresh endpoint if you add one to server.py.
  try {
    const resp = UrlFetchApp.fetch(`${CONFIG.SERVER_URL}/health`, {
      muteHttpExceptions: true,
      method: "get",
    });
    Logger.log("Server health: %s", resp.getContentText());
  } catch (e) {
    Logger.log("Could not reach server: %s", e.message);
  }
}
