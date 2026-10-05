"""Gacha cards and banners ORM models."""

from sqlalchemy import Boolean, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from dankagu.core.database import Base


class GachaCard(Base):
    """Card definition in the gacha pool."""

    __tablename__ = "gacha_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rarity: Mapped[int] = mapped_column(Integer, index=True)  # 1=N, 2=R, 3=SR, 4=SSR
    element: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0=None, 1=Em, 2=Co, 3=Ro
    chara_id: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(128), default="")
    second_name: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    def __repr__(self) -> str:
        return f"<GachaCard id={self.id} rarity={self.rarity} element={self.element}>"


class GachaBanner(Base):
    """Gacha banner configuration."""

    __tablename__ = "gacha_banners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Gacha ID (e.g. 159, 163, 3)
    name: Mapped[str] = mapped_column(String(128))
    element: Mapped[int] = mapped_column(Integer, default=0)  # 0=All, 1=Em, 2=Co, 3=Ro
    guaranteed_ssr: Mapped[bool] = mapped_column(Boolean, default=False)
    pickups: Mapped[list[int]] = mapped_column(JSON, default=list)  # List of pickup card IDs
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    def __repr__(self) -> str:
        return f"<GachaBanner id={self.id} name={self.name!r} elem={self.element} guaranteed_ssr={self.guaranteed_ssr}>"
