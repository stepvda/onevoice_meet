from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


# (The legacy `/v1/me` whoami endpoint that previously lived here has been
# removed — it shadowed the full-shape `/v1/me` in auth_native.py because
# health is registered first. Use `/v1/me` from auth_native.py for the
# canonical user info, including kind / is_admin / is_voucher_admin.)
