import json
from database import SessionLocal
from models import User

def backup_users():
    db = SessionLocal()
    try:
        users = db.query(User).all()
        user_list = []
        for u in users:
            user_list.append({
                "id": u.id, "email": u.email, "full_name": u.full_name,
                "plan": u.plan, "credits": u.credits, 
                "subscription_status": u.subscription_status
            })
            
        with open("prod_users_backup.json", "w") as f:
            json.dump(user_list, f, indent=4)
            
        print(f"✅ Successfully backed up {len(users)} users to prod_users_backup.json")
    finally:
        db.close()

if __name__ == "__main__":
    backup_users()