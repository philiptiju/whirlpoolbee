/**
 * Shared datetime helpers.
 *
 * The server always SAVES and RETURNS timestamps in UTC (ISO-8601, e.g.
 * "2026-09-09T05:11:07.341Z"). These helpers convert them to the viewer's
 * local browser/OS timezone for display, consistently across every page,
 * instead of showing the raw UTC string.
 */

function formatLocalDateTime(isoString, options) {
    if (!isoString) return 'N/A';
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleString(undefined, options || {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
}

function formatLocalDate(isoString) {
    if (!isoString) return 'N/A';
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleDateString();
}

function formatLocalTimeOnly(isoString) {
    if (!isoString) return 'N/A';
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleTimeString();
}

/**
 * Progressive-enhancement pass: any element the server rendered with a raw
 * UTC timestamp in a `data-utc` attribute gets its text replaced with the
 * local-time equivalent. Use `data-utc-mode="date"` for a date-only element,
 * defaults to full date+time otherwise. Safe to call repeatedly (e.g. after
 * injecting new rows via fetch()).
 */
function autoLocalizeTimestamps(root) {
    (root || document).querySelectorAll('[data-utc]').forEach(el => {
        const utc = el.getAttribute('data-utc');
        if (!utc) return;
        const mode = el.getAttribute('data-utc-mode') || 'datetime';
        el.textContent = mode === 'date' ? formatLocalDate(utc) : formatLocalDateTime(utc);
    });
}

document.addEventListener('DOMContentLoaded', () => autoLocalizeTimestamps());
