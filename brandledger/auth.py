"""演示用身份配置。

生产环境应替换为真实的身份提供方（SSO/IAM），此处用静态令牌演示按职责的访问控制。
"""

# token -> {"actor": 操作人, "role": 角色}
USERS = {
    "tok-admin": {"actor": "admin", "role": "admin"},
    "tok-legal": {"actor": "legal", "role": "legal"},
    "tok-commercial": {"actor": "commercial", "role": "commercial"},
    "tok-finance": {"actor": "finance", "role": "finance"},
}


def authenticate(token):
    return USERS.get(token)
