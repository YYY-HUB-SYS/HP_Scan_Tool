"""
WIA 扫描引擎 — HP Scan Tool v4.0 — 降级方案
参考 pyautoscan：当 eSCL 不可用时，使用 Windows WIA 驱动扫描。

返回类型与 eSCL 引擎对齐：wia_scan / try_wia_scan 均返回 (bytes, ext)。
"""

import logging
import os
import tempfile
from typing import Optional

__all__ = ["list_wia_scanners", "wia_scan", "try_wia_scan"]

logger = logging.getLogger(__name__)

# WIA 格式 GUID 映射（WIA COM API 专用，不可与 FORMAT_MIME 合并）
_WIA_FORMAT_GUID = {
    "jpg": "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}",
    "jpeg": "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}",
    "png": "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}",
    "tiff": "{B96B3CB1-0728-11D3-9D7B-0000F81EF32E}",
    "bmp": "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}",
}


def _get_wia_device_manager():
    """获取 WIA DeviceManager COM 对象"""
    try:
        import win32com.client
        return win32com.client.Dispatch("WIA.DeviceManager")
    except Exception as e:
        logger.debug("WIA DeviceManager 不可用: %s", e)
        return None


def list_wia_scanners() -> list[dict]:
    """列出所有 WIA 扫描仪"""
    dm = _get_wia_device_manager()
    if not dm:
        return []

    scanners = []
    for i in range(dm.DeviceInfos.Count):
        info = dm.DeviceInfos[i + 1]
        try:
            props = {p.Name: str(p.Value) for p in info.Properties if p.Name and p.Value}
            scanners.append({
                "id": info.DeviceID,
                "name": props.get("Name", f"Scanner {i+1}"),
                "type": props.get("Type", ""),
                "description": props.get("Description", ""),
            })
        except Exception as e:
            logger.debug("WIA 设备信息读取失败 [%d]: %s", i, e)
    return scanners


def _connect_device(device_id: str):
    """连接 WIA 设备，返回 Device 对象"""
    dm = _get_wia_device_manager()
    if not dm:
        raise RuntimeError("WIA 不可用，请确保安装了 pywin32")

    for info in dm.DeviceInfos:
        if info.DeviceID == device_id:
            return info.Connect()

    # 降级：尝试首个扫描仪
    for info in dm.DeviceInfos:
        if info.Type == 1:  # ScannerDeviceType
            return info.Connect()

    raise RuntimeError("未找到 WIA 扫描仪")


def _set_item_props(item, resolution: int, color_mode: int):
    """设置 WIA Item 的扫描参数"""
    if not hasattr(item, "Properties"):
        return
    prop_map = {
        "Horizontal Resolution": resolution,
        "Vertical Resolution": resolution,
        "Current Intent": color_mode,
        "Brightness": 0,
        "Contrast": 0,
    }
    for prop in item.Properties:
        if prop.Name in prop_map:
            try:
                prop.Value = prop_map[prop.Name]
            except Exception as e:
                logger.debug("WIA 属性设置失败 [%s]: %s", prop.Name, e)


def _save_wia_image(img, ext: str) -> bytes:
    """
    将 WIA Image 对象转换为 bytes。
    先尝试 WIA ImageProcess 格式转换，再保存到临时文件，最后用 PIL 读取字节。
    """
    # 尝试 WIA 格式转换
    try:
        if ext in _WIA_FORMAT_GUID:
            from win32com.client import Dispatch
            img_proc = Dispatch("WIA.ImageProcess")
            img_proc.Filters.Add(img_proc.FilterInfos["Convert"].FilterID)
            img_proc.Filters[1].Properties["FormatID"].Value = _WIA_FORMAT_GUID[ext]
            img = img_proc.Apply(img)
    except Exception:
        pass  # 格式转换失败，用原始 img 继续

    # 保存到临时文件再读取字节
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".{ext}")
    os.close(tmp_fd)
    try:
        try:
            img.SaveFile(tmp_path)
        except Exception:
            # 降级：用 PIL 从 FileData 读取
            from PIL import Image
            import io
            if hasattr(img, "FileData"):
                guid = _WIA_FORMAT_GUID.get(ext, _WIA_FORMAT_GUID["jpg"])
                data = img.FileData.GetFile(FormatID=guid)
                pil_img = Image.open(io.BytesIO(data.BinaryData))
                pil_img.save(tmp_path)
            else:
                raise

        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def wia_scan(
    device_id: str,
    resolution: int = 300,
    color_mode: int = 1,  # 1=Color, 2=Grayscale, 4=BlackWhite
    output_format: str = "jpg",
) -> tuple[bytes, str]:
    """
    使用 WIA 执行扫描，返回 (数据, 扩展名)。
    与 eSCL execute_scan 返回类型对齐。
    """
    ext = output_format.lower()
    device = _connect_device(device_id)

    errors = []
    for item_idx, item in enumerate(device.Items):
        _set_item_props(item, resolution, color_mode)

        try:
            img = item.Transfer()
        except Exception as e:
            errors.append(f"Item {item_idx} Transfer 失败: {e}")
            logger.debug("WIA Item %d Transfer 失败: %s", item_idx, e)
            continue

        try:
            data = _save_wia_image(img, ext)
            return data, ext
        except Exception as e:
            errors.append(f"Item {item_idx} 保存失败: {e}")
            logger.debug("WIA Item %d 保存失败: %s", item_idx, e)
            continue

    if errors:
        raise RuntimeError(f"WIA 扫描失败（尝试 {len(device.Items)} 个项均失败）: {'; '.join(errors)}")
    raise RuntimeError("扫描仪无可用扫描项")


def try_wia_scan(
    resolution: int = 300,
    color_mode_name: str = "Color",
    output_format: str = "jpg",
) -> Optional[tuple[bytes, str]]:
    """
    尝试用 WIA 扫描（使用第一个可用扫描仪）。
    成功返回 (bytes, ext)，失败返回 None。
    """
    scanners = list_wia_scanners()
    if not scanners:
        logger.debug("WIA: 未发现扫描仪")
        return None

    scanner = scanners[0]
    color_map = {"Color": 1, "Grayscale": 2, "BlackAndWhite": 4, "RGB24": 1, "Grayscale8": 2}
    cm = color_map.get(color_mode_name, 1)

    try:
        return wia_scan(
            device_id=scanner["id"],
            resolution=resolution,
            color_mode=cm,
            output_format=output_format,
        )
    except Exception as e:
        logger.debug("WIA 扫描失败 [%s]: %s", scanner.get("name", "?"), e)
        return None
