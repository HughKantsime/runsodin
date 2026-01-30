# User Management Integration Guide

## Overview
Pre-built components for authentication and user management.
These files exist but are NOT wired up. Follow these steps to enable.

## Files Created
- `backend/auth.py` - Password hashing, JWT tokens, permission checks
- `backend/migrations/add_users.py` - Users table schema
- `frontend/src/pages/Login.jsx` - Login page component
- `frontend/src/pages/Admin.jsx` - User CRUD admin page

---

## Step 1: Install Dependencies

```bash
pip install passlib python-jose bcrypt --break-system-packages
```

---

## Step 2: Run Migration

```bash
cd /opt/printfarm-scheduler/backend
python3 migrations/add_users.py
```

---

## Step 3: Create Initial Admin User

Edit `migrations/add_users.py` and uncomment the last line, then run:

```bash
python3 -c "
import sys
sys.path.insert(0, '/opt/printfarm-scheduler/backend')
from migrations.add_users import create_admin
create_admin('admin', 'admin@yourcompany.com', 'YourSecurePassword123')
"
```

---

## Step 4: Add Auth Endpoints to Backend

Add to `main.py` after imports:

```python
from auth import (
    Token, UserCreate, UserResponse, 
    verify_password, hash_password, create_access_token, decode_token, has_permission
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    token_data = decode_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.execute(text("SELECT * FROM users WHERE username = :username"), 
                      {"username": token_data.username}).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(user._mapping)

def require_role(required_role: str):
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if not has_permission(current_user['role'], required_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return role_checker
```

Add auth endpoints:

```python
@app.post("/api/auth/login", response_model=Token, tags=["Auth"])
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.execute(text("SELECT * FROM users WHERE username = :username"), 
                      {"username": form_data.username}).fetchone()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    
    # Update last login
    db.execute(text("UPDATE users SET last_login = :now WHERE id = :id"), 
               {"now": datetime.now(), "id": user.id})
    db.commit()
    
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/users", tags=["Users"])
async def list_users(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    users = db.execute(text("SELECT id, username, email, role, is_active, last_login, created_at FROM users")).fetchall()
    return [dict(u._mapping) for u in users]

@app.post("/api/users", tags=["Users"])
async def create_user(user: UserCreate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    password_hash = hash_password(user.password)
    db.execute(text("""
        INSERT INTO users (username, email, password_hash, role) 
        VALUES (:username, :email, :password_hash, :role)
    """), {"username": user.username, "email": user.email, "password_hash": password_hash, "role": user.role})
    db.commit()
    return {"status": "created"}

@app.patch("/api/users/{user_id}", tags=["Users"])
async def update_user(user_id: int, updates: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if 'password' in updates and updates['password']:
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)
    
    set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
    updates['id'] = user_id
    db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
    db.commit()
    return {"status": "updated"}

@app.delete("/api/users/{user_id}", tags=["Users"])
async def delete_user(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()
    return {"status": "deleted"}
```

---

## Step 5: Add Frontend Routes

In `App.jsx`, add imports:

```javascript
import Login from './pages/Login'
import Admin from './pages/Admin'
```

Add routes:

```javascript
<Route path="/login" element={<Login />} />
<Route path="/admin" element={<Admin />} />
```

Add nav item (admin only):

```javascript
<NavItem to="/admin" icon={Shield}>Admin</NavItem>
```

---

## Step 6: Add Auth Context (Optional but Recommended)

Create `frontend/src/context/AuthContext.jsx` for managing auth state across the app.

---

## Step 7: Protect API Calls

Update `api.js` to include JWT token:

```javascript
const getAuthHeaders = () => {
  const token = localStorage.getItem('token')
  return token ? { 'Authorization': `Bearer ${token}` } : {}
}

// Add to fetchAPI function:
headers: {
  'Content-Type': 'application/json',
  ...getAuthHeaders(),
  ...options.headers,
}
```

---

## Step 8: Restart Services

```bash
sudo systemctl restart printfarm-backend
cd /opt/printfarm-scheduler/frontend && npm run build
```

---

## Role Permissions

| Role | View | Schedule | Start/Cancel | Admin |
|------|------|----------|--------------|-------|
| viewer | ✓ | ✗ | ✗ | ✗ |
| operator | ✓ | ✓ | ✓ | ✗ |
| admin | ✓ | ✓ | ✓ | ✓ |

---

## Security Notes

1. Change `JWT_SECRET_KEY` in production:
   ```bash
   export JWT_SECRET_KEY="your-very-long-random-secret-key"
   ```

2. Use HTTPS in production (nginx reverse proxy)

3. Set secure password requirements

4. Consider session timeout settings
