from html import escape
from textwrap import wrap


CARD_WIDTH = 1200
CARD_HEIGHT = 630


def _safe(value) -> str:
    return escape(str(value or "Unavailable"), quote=True)


def _line_text(value, width=54, limit=2) -> list[str]:
    text = " ".join(str(value or "Unavailable").split())
    lines = wrap(text, width=width, break_long_words=True, break_on_hyphens=False) or ["Unavailable"]
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = lines[-1].rstrip(" .") + "..."
    return lines


def _text_block(lines, x, y, size, *, color="#f5f7fa", weight=600, line_height=None, anchor=None) -> str:
    line_height = line_height or int(size * 1.25)
    tspans = []
    for index, line in enumerate(lines):
        dy = 0 if index == 0 else line_height
        tspans.append(f'<tspan x="{x}" dy="{dy}">{_safe(line)}</tspan>')
    anchor_markup = f' text-anchor="{anchor}"' if anchor else ""
    return (
        f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
        f'font-family="Inter,Segoe UI,Arial,sans-serif" font-weight="{weight}"{anchor_markup}>'
        + "".join(tspans)
        + "</text>"
    )


def _metric(x, label, value) -> str:
    return (
        f'<rect x="{x}" y="360" width="220" height="112" rx="12" fill="#111922" stroke="#2d3946"/>'
        + _text_block([label.upper()], x + 20, 396, 15, color="#8fa5ba", weight=600)
        + _text_block(_line_text(value, width=20, limit=2), x + 20, 438, 25, weight=700)
    )


def _document(kicker, title, subtitle, metrics, signals, footer, destination="bashops.site") -> str:
    metric_markup = "".join(_metric(54 + index * 236, label, value) for index, (label, value) in enumerate(metrics[:4]))
    signal_text = "  |  ".join(str(value) for value in signals[:5] if value) or "Public GitHub evidence"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_WIDTH}" height="{CARD_HEIGHT}" viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}" role="img" aria-label="{_safe(title)}">
<rect width="1200" height="630" fill="#071018"/>
<rect x="28" y="28" width="1144" height="574" rx="18" fill="#0c151f" stroke="#2d3946"/>
<rect x="28" y="28" width="8" height="574" rx="4" fill="#ff9800"/>
<circle cx="80" cy="82" r="25" fill="#071018" stroke="#ff9800"/>
{_text_block([">_"], 64, 91, 20, color="#ff9800", weight=700)}
{_text_block([kicker.upper()], 120, 76, 16, color="#ffb13b", weight=700)}
{_text_block(["BashOps Radar"], 120, 104, 20, weight=700)}
{_text_block(_line_text(title, width=42, limit=2), 54, 194, 45, weight=750, line_height=54)}
{_text_block(_line_text(subtitle, width=82, limit=2), 54, 304, 22, color="#a9bed0", weight=400, line_height=30)}
{metric_markup}
{_text_block(["SIGNALS"], 54, 522, 14, color="#ffb13b", weight=700)}
{_text_block(_line_text(signal_text, width=105, limit=1), 54, 552, 18, color="#d5e0e9", weight=500)}
{_text_block([footer], 54, 582, 15, color="#8fa5ba", weight=500)}
{_text_block([destination], 1140, 582, 15, color="#ffb13b", weight=700, anchor="end")}
</svg>'''


def render_developer_profile_card(profile) -> str:
    activity = profile.profile_data or {}
    strengths = profile.strength_data or {}
    skills = [item.get("label") for item in strengths.get("categories") or [] if item.get("label")]
    skills.extend(item.get("label") for item in strengths.get("languages") or [] if item.get("label"))
    metrics = [
        ("Repositories", activity.get("repositories_contributed_to", 0)),
        ("Pull Requests", activity.get("public_pull_requests_found", 0)),
        ("Merged PRs", activity.get("merged_pull_requests_found", 0)),
        ("Issues", activity.get("public_issues_found", 0)),
    ]
    return _document(
        "Developer Proof-of-Work",
        profile.display_name,
        f"@{profile.github_username} | Public contribution evidence",
        metrics,
        skills,
        "Generated from public GitHub activity.",
        f"bashops.site/developer/{profile.public_slug}",
    )


def render_opportunity_card(
    item,
    *,
    contract_potential="Unavailable",
    potential_label="Contract Potential",
    heading="Today's OSS Opportunity",
    reason="",
    destination="bashops.site/today",
) -> str:
    signals = [item.primary_language, *(item.categories or [])]
    return _document(
        heading,
        item.repository_full_name,
        reason or item.public_reason,
        [
            ("Radar Score", f"{int(round(item.radar_score))}/100"),
            ("Difficulty", item.difficulty),
            ("Merge Probability", item.merge_probability),
            (potential_label, contract_potential),
        ],
        signals,
        "Generated from public GitHub activity.",
        destination,
    )
