"""
页面元数据 — HP Scan Tool v4.0
记录每页的原始几何与处理参数，使纠偏/续扫/双面/大批量合并可回溯。
"""

import json
import os
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class PageMeta:
    """单页元数据。cache_path 指向**永不修改**的原始扫描件。"""
    cache_path: str
    width: int
    height: int
    manual_rotation: int = 0        # 用户手动旋转 0/90/180/270
    deskew_angle: float = 0.0       # 自动纠偏角（度），批次 4 使用
    a4_normalized: bool = False     # 落盘时是否做过 A4 画布归一
    crop_rect: Optional[tuple] = None   # 自动裁边框


class PageMetaStore:
    """按 job 持久化页元数据（原子写：.tmp + os.replace）"""

    def __init__(self, cache_root: str):
        self._path = os.path.join(cache_root, "page_meta.json")

    def load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, metas: dict) -> None:
        tmp = self._path + ".tmp"
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(metas, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def set_page(self, job_id: str, page_no: int, meta: PageMeta) -> None:
        metas = self.load()
        metas.setdefault(job_id, {})[str(page_no)] = asdict(meta)
        self.save(metas)

    def get_job(self, job_id: str) -> dict:
        return self.load().get(job_id, {})

    def remove_job(self, job_id: str) -> None:
        metas = self.load()
        if job_id in metas:
            del metas[job_id]
            self.save(metas)
