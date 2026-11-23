import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from content_schemas import TemplateIn
from database import get_db
from models import User, PromoTemplate
from routers.admin_router import get_current_admin

router = APIRouter(tags=["Promo templates"])

MAX_TEMPLATES = 100


def _out(t: PromoTemplate) -> dict:
    return {"id": t.id, "kind": t.kind, "name": t.name, "emoji": t.emoji, "end_in_days": t.end_in_days,
            "data": json.loads(t.data), "created_at": t.created_at.isoformat() + "Z" if t.created_at else None}


@router.get("/admin/promo-templates")
def list_templates(kind: Optional[str] = Query(None, pattern="^(popup|banner)$"), db: Session = Depends(get_db),
                   admin: User = Depends(get_current_admin)):
    q = db.query(PromoTemplate)
    if kind:
        q = q.filter(PromoTemplate.kind == kind)
    return {"templates": [_out(t) for t in q.order_by(PromoTemplate.id.desc()).all()]}


@router.post("/admin/promo-templates", status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    if db.query(PromoTemplate).count() >= MAX_TEMPLATES:
        raise HTTPException(status_code=409, detail=f"At most {MAX_TEMPLATES} saved templates. Delete one first.")
    t = PromoTemplate(kind=body.kind, name=body.name, emoji=body.emoji, end_in_days=body.end_in_days,
                      data=json.dumps(body.data), created_by_user_id=admin.id)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _out(t)


@router.delete("/admin/promo-templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    t = db.get(PromoTemplate, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(t)
    db.commit()
    return {"status": "deleted", "id": template_id}
