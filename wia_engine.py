"""
WIA 扫描引擎 — 降级方案
参考 pyautoscan：当 eSCL 不可用时，使用 Windows WIA 驱动扫描。
"""

import logging
import os
import sys
from typing import Optional

__all__ = ["list_wia_scanners", "wia_scan_to_file", "try_wia_scan"]

logger = logging.getLogger(__name__)


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


def wia_scan_to_file(
    device_id: str,
    output_path: str,
    resolution: int = 300,
    color_mode: int = 1,  # 1=Color, 2=Grayscale, 4=BlackWhite
    output_format: str = "jpg",
) -> str:
    """
    使用 WIA 执行扫描并保存文件。
    color_mode: 1=Color, 2=Grayscale, 4=BlackAndWhite
    """
    dm = _get_wia_device_manager()
    if not dm:
        raise RuntimeError("WIA 不可用，请确保安装了 pywin32")

    # 查找设备
    device = None
    for info in dm.DeviceInfos:
        if info.DeviceID == device_id:
            device = info.Connect()
            break

    if not device:
        # 尝试首个扫描仪
        for info in dm.DeviceInfos:
            if info.Type == 1:  # ScannerDeviceType
                device = info.Connect()
                break

    if not device:
        raise RuntimeError("未找到 WIA 扫描仪")

    # 格式 GUID 映射
    ext = output_format.lower()
    format_guid_map = {
        "jpg": "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}",
        "jpeg": "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}",
        "png": "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}",
        "tiff": "{B96B3CB1-0728-11D3-9D7B-0000F81EF32E}",
        "bmp": "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}",
    }

    final_path = output_path
    if not final_path.lower().endswith(f".{ext}"):
        final_path = f"{final_path}.{ext}"

    # 遍历所有 Items，尝试每一个直到成功（A5 修复：原代码在首次迭代就 return）
    errors = []
    for item_idx, item in enumerate(device.Items):
        # 设置参数
        if hasattr(item, "Properties"):
            for prop in item.Properties:
                try:
                    if prop.Name == "Horizontal Resolution":
                        prop.Value = resolution
                    elif prop.Name == "Vertical Resolution":
                        prop.Value = resolution
                    elif prop.Name == "Current Intent":
                        prop.Value = color_mode
                    elif prop.Name == "Brightness":
                        prop.Value = 0
                    elif prop.Name == "Contrast":
                        prop.Value = 0
                except Exception as e:
                    logger.debug("WIA 属性设置失败 [%s]: %s", prop.Name, e)

        # 执行扫描
        try:
            img = item.Transfer()
        except Exception as e:
            errors.append(f"Item {item_idx} Transfer 失败: {e}")
            logger.debug("WIA Item %d Transfer 失败: %s", item_idx, e)
            continue  # 尝试下一个 item

        # 保存
        try:
            if ext in format_guid_map:
                from win32com.client import Dispatch
                img_proc = Dispatch("WIA.ImageProcess")  # Q1: ip → img_proc
                img_proc.Filters.Add(img_proc.FilterInfos["Convert"].FilterID)
                img_proc.Filters[1].Properties["FormatID"].Value = format_guid_map[ext]
                img = img_proc.Apply(img)
            img.SaveFile(final_path)
        except Exception:
            # 降级：用 PIL
            try:
                import io
                from PIL import Image

                if hasattr(img, "FileData"):
                    data = img.FileData.GetFile(FormatID=format_guid_map.get(ext, format_guid_map["jpg"]))
                    pil_img = Image.open(io.BytesIO(data.BinaryData))
                    pil_img.save(final_path)
                else:
                    raise
            except Exception:
                try:
                    img.SaveFile(final_path)
                except Exception as e:
                    errors.append(f"Item {item_idx} 保存失败: {e}")
                    logger.debug("WIA Item %d 保存失败: %s", item_idx, e)
                    continue  # 尝试下一个 item

        return final_path  # 成功

    # 所有 item 都失败
    if errors:
        raise RuntimeError(f"WIA 扫描失败（尝试 {len(device.Items)} 个项均失败）: {'; '.join(errors)}")
    raise RuntimeError("扫描仪无可用扫描项")


def try_wia_scan(
    output_path: str,
    resolution: int = 300,
    color_mode_name: str = "Color",
    output_format: str = "jpg",
) -> Optional[str]:
    """
    尝试用 WIA 扫描（使用第一个可用扫描仪）。
    成功返回文件路径，失败返回 None。
    """
    scanners = list_wia_scanners()
    if not scanners:
        logger.debug("WIA: 未发现扫描仪")
        return None

    scanner = scanners[0]
    color_map = {"Color": 1, "Grayscale": 2, "BlackAndWhite": 4, "RGB24": 1, "Grayscale8": 2}
    cm = color_map.get(color_mode_name, 1)

    try:
        return wia_scan_to_file(
            device_id=scanner["id"],
            output_path=output_path,
            resolution=resolution,
            color_mode=cm,
            output_format=output_format,
        )
    except Exception as e:
        logger.debug("WIA 扫描失败 [%s]: %s", scanner.get("name", "?"), e)
        return None
