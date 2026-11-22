"""Admin-managed "What's New" modal: serialisation, ordering, the auto-announce rule and the built-in defaults."""
import json
from typing import List, Optional

from sqlalchemy.orm import Session

from models import WhatsNewRelease

PLAY_STORE = "https://play.google.com/store/apps/details?id=com.shin.depthflow"

# The content the modal shipped with, so an admin can import it once and then edit it.
DEFAULT_RELEASES = [
    {
        "tab_label": "Latest: App Launch",
        "badge_text": "New App Launch \U0001F389",
        "title": "Depthflow is now on Mobile! \U0001F4F1",
        "subtitle": "Experience the power of AI depth motion generation right from your pocket. "
                    "Now officially available on Android.",
        "features": [
            {"icon": "smartphone", "title": "Pocket-Sized Power",
             "description": "Generate immersive 3D videos anytime, anywhere directly from your Android device."},
            {"icon": "devices", "title": "Cross-Platform Sync",
             "description": "Your credits, workspaces, and generated videos sync seamlessly between web and mobile."},
            {"icon": "zap", "title": "Lightning Fast Creation",
             "description": "Optimized mobile interface designed specifically for quick edits and instant rendering."},
            {"icon": "gift", "title": "Exclusive Mobile Perks",
             "description": "Download the app today and unlock special bonus credits to kickstart your mobile journey."},
        ],
        "button1_text": "Google Play", "button1_link": PLAY_STORE, "button1_style": "google_play",
        "button2_text": "Continue to Web", "button2_link": None,
    },
    {
        "tab_label": "Version 2.0 Updates",
        "badge_text": "Previous Update",
        "title": "Depthflow 2.0 is Here \U0001F680",
        "subtitle": "A faster, smarter, and more powerful way to turn your 2D images into stunning 3D motion videos.",
        "features": [
            {"icon": "gift", "title": "Free Plan Introduced",
             "description": "Start with a free render and explore Depthflow before upgrading."},
            {"icon": "coins", "title": "More Credits, Better Pricing",
             "description": "Get higher value with increased credits and optimized pricing plans."},
            {"icon": "code", "title": "API Access for Developers",
             "description": "Integrate 2D to 3D rendering directly into your apps, tools, or workflows."},
            {"icon": "toggle", "title": "New Workspace Experience",
             "description": "Switch between Basic Mode for quick edits and Advanced Mode for full control."},
            {"icon": "film", "title": "Extended Video Duration",
             "description": "Create cinematic 3D videos up to 30 seconds with smooth depth motion."},
            {"icon": "sliders", "title": "Advanced Controls & Customization",
             "description": "Fine-tune depth, motion, and rendering settings with multiple new options."},
            {"icon": "rocket", "title": "Faster Rendering Engine",
             "description": "Optimized performance for quicker results and smoother processing."},
        ],
        "button1_text": "Go to Workspace", "button1_link": "/workspace-2_0", "button1_style": "dark",
        "button2_text": "View Pricing", "button2_link": "/pricing",
    },
]


def _iso(dt) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def features_of(r: WhatsNewRelease) -> List[dict]:
    try:
        data = json.loads(r.features or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def serialize_public(r: WhatsNewRelease) -> dict:
    return {"id": r.id, "tab_label": r.tab_label, "badge_text": r.badge_text, "title": r.title,
            "subtitle": r.subtitle or "", "features": features_of(r),
            "button1_text": r.button1_text, "button1_link": r.button1_link, "button1_style": r.button1_style or "light",
            "button2_text": r.button2_text, "button2_link": r.button2_link}


def serialize_admin(r: WhatsNewRelease) -> dict:
    d = serialize_public(r)
    d.update({"status": r.status, "auto_show": bool(r.auto_show), "display_order": r.display_order,
              "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at)})
    return d


def ordered_active(db: Session) -> List[WhatsNewRelease]:
    return (db.query(WhatsNewRelease).filter(WhatsNewRelease.status == "ACTIVE")
            .order_by(WhatsNewRelease.display_order.asc(), WhatsNewRelease.id.desc()).all())


def announce_version(db: Session) -> Optional[int]:
    """Browsers open the modal once whenever this number changes: the newest live release flagged `auto_show`.
    Editing a release does not change it; publishing a new auto-show release does."""
    r = (db.query(WhatsNewRelease).filter(WhatsNewRelease.status == "ACTIVE", WhatsNewRelease.auto_show == True)  # noqa: E712
         .order_by(WhatsNewRelease.id.desc()).first())
    return r.id if r else None


def import_defaults(db: Session, admin_id: Optional[int] = None) -> List[WhatsNewRelease]:
    """Seed the two built-in releases. Only when nothing exists yet, so it can never duplicate or overwrite."""
    if db.query(WhatsNewRelease).count() > 0:
        return []
    created = []
    for i, spec in enumerate(DEFAULT_RELEASES):
        row = WhatsNewRelease(
            tab_label=spec["tab_label"], badge_text=spec["badge_text"], title=spec["title"], subtitle=spec["subtitle"],
            features=json.dumps(spec["features"]), button1_text=spec["button1_text"], button1_link=spec["button1_link"],
            button1_style=spec["button1_style"], button2_text=spec["button2_text"], button2_link=spec["button2_link"],
            auto_show=False, status="ACTIVE", display_order=i, created_by_user_id=admin_id)
        db.add(row)
        created.append(row)
    db.commit()
    return created
