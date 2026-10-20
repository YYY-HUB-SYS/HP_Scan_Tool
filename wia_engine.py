"""
WIA 扫描引擎 — HP Scan Tool v4.0 — 降级方案
参考 pyautoscan：当 eSCL 不可用时，使用 Windows WIA 驱动扫描。

返回类型与 eSCL 引擎对齐：wia_scan / try_wia_scan 均返回 (bytes, ext)。
"""

import logging
import os
import tempfile
from typing import Optional

# 复用协议层的唯一事实源：
#   - REQUEST_FORMAT：输出格式 → 落盘扩展名的**归一**规则（jpeg→jpg、tif→tiff）
#     —— WIA 侧原来直接 lower() 回显，导致同一用户意图在两条引擎路径
#        落盘的扩展名不同（.jpeg vs .jpg、.tif vs .tiff）。
#   - ScanCancelled：三条引擎路径统一的取消信号（协调器靠它区分
#     "取消"与"失败"，发 scan_cancelled 而不是 scan_failed）。
from escl_engine import REQUEST_FORMAT, ScanCancelled

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


def _normalize_ext(output_format: str) -> str:
    """
    把用户选的输出格式归一成落盘扩展名（与 eSCL 路径**同一张表**）。

    REQUEST_FORMAT 是协议层的唯一事实源：jpeg→jpg、tif→tiff。
    不在表里的格式（如 bmp —— WIA 自己的 GUID 表支持它）保持原样回显，
    交给 WIA 的格式表决定。
    """
    fmt = (output_format or "jpg").lower()
    return REQUEST_FORMAT.get(fmt, (None, fmt))[1]


def _check_cancel(cancel_event):
    """
    取消信号检查点。已取消 ⇒ 抛 ScanCancelled。

    与 eSCL 路径的 cancel_event 语义一致：取消抛异常（不是返回 None），
    调用方据此发 scan_cancelled 而不是 scan_failed。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ScanCancelled("WIA 扫描已取消")


def _save_wia_image(img, ext: str, cancel_event=None) -> bytes:
    """
    将 WIA Image 对象转换为 bytes。
    先尝试 WIA ImageProcess 格式转换，再保存到临时文件，最后用 PIL 读取字节。

    ★ 新契约（ext=pdf ⇒ 字节是**图片**，PDF 由 save_manager 本地合成）：
      WIA 不支持 .pdf 扩展名，SaveFile("*.pdf") 必然失败，
      旧实现走 FileData 兜底后 `pil_img.save(tmp_path)` 会让 PIL 按 .pdf
      扩展名写出**真 PDF**字节 ⇒ 缓存页 page_001.pdf 打不开（预览无法渲染）。
      这里对 pdf 内部按 jpg 处理（WIA 支持 jpg），保证交出去的是图片字节。
    """
    # 内部实际使用的格式：pdf → jpg（图片字节），其余原样
    save_ext = "jpg" if ext == "pdf" else ext

    # 格式转换（ImageProcess）也可能耗时 —— 转换前先看取消信号
    _check_cancel(cancel_event)

    # 尝试 WIA 格式转换
    try:
        if save_ext in _WIA_FORMAT_GUID:
            from win32com.client import Dispatch
            img_proc = Dispatch("WIA.ImageProcess")
            img_proc.Filters.Add(img_proc.FilterInfos["Convert"].FilterID)
            img_proc.Filters[1].Properties["FormatID"].Value = _WIA_FORMAT_GUID[save_ext]
            img = img_proc.Apply(img)
    except ScanCancelled:
        raise
    except Exception:
        pass  # 格式转换失败，用原始 img 继续

    # 保存到临时文件再读取字节
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".{save_ext}")
    os.close(tmp_fd)
    try:
        # 写盘是耗时点 —— 落笔前先看取消信号
        _check_cancel(cancel_event)
        try:
            img.SaveFile(tmp_path)
        except ScanCancelled:
            raise
        except Exception:
            # 降级：用 PIL 从 FileData 读取
            from PIL import Image
            import io
            if hasattr(img, "FileData"):
                guid = _WIA_FORMAT_GUID.get(save_ext, _WIA_FORMAT_GUID["jpg"])
                data = img.FileData.GetFile(FormatID=guid)
                pil_img = Image.open(io.BytesIO(data.BinaryData))
                pil_img.save(tmp_path)
            else:
                raise

        with open(tmp_path, "rb") as f:
            out = f.read()
        # 读盘完成后再查一次：避免取消后仍把数据交给上层
        _check_cancel(cancel_event)
        return out
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def wia_scan(
    device_id: str,
    resolution: int = 300,
    color_mode: int = 1,  # 1=Color, 2=Grayscale, 4=BlackWhite
    output_format: str = "jpg",
    cancel_event=None,
) -> tuple[bytes, str]:
    """
    使用 WIA 执行扫描，返回 (数据, 扩展名)。
    与 eSCL execute_scan 返回类型对齐。

    cancel_event：可选的取消信号（threading.Event），语义与 eSCL 路径一致：
      - 取消时抛 ScanCancelled（**不是**返回 None）—— 调用方据此区分
        "用户取消"与"扫描失败"（协调器发 scan_cancelled 而不是 scan_failed）；
      - 检查点覆盖：连接设备前、每个扫描项前后、COM Transfer() 返回后、
        格式转换前、写盘前、读盘后。
        COM 的 Transfer() 本身是阻塞调用，**无法**在调用中途打断，
        只能在它返回后立即响应取消（这是 WIA COM 的固有限制）。
    """
    ext = _normalize_ext(output_format)
    _check_cancel(cancel_event)
    device = _connect_device(device_id)

    errors = []
    for item_idx, item in enumerate(device.Items):
        # 每个扫描项开始前检查取消（跳过剩余项）
        _check_cancel(cancel_event)
        _set_item_props(item, resolution, color_mode)
        _check_cancel(cancel_event)

        try:
            img = item.Transfer()
        except ScanCancelled:
            raise
        except Exception as e:
            errors.append(f"Item {item_idx} Transfer 失败: {e}")
            logger.debug("WIA Item %d Transfer 失败: %s", item_idx, e)
            continue

        # Transfer() 返回后立刻查取消：用户在这段等待里点了取消
        _check_cancel(cancel_event)

        try:
            data = _save_wia_image(img, ext, cancel_event=cancel_event)
            return data, ext
        except ScanCancelled:
            # 取消不是"这一项失败"——必须往上抛，不能吞成 errors
            raise
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
    cancel_event=None,
) -> Optional[tuple[bytes, str]]:
    """
    尝试用 WIA 扫描（使用第一个可用扫描仪）。
    成功返回 (bytes, ext)，失败返回 None。

    cancel_event：取消信号。**取消时抛 ScanCancelled**（不是返回 None）——
    取消与失败必须能区分开，否则调用方会把用户的取消动作报成扫描错误。
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
            cancel_event=cancel_event,
        )
    except ScanCancelled:
        # 取消不是失败：原样往上抛
        raise
    except Exception as e:
        logger.debug("WIA 扫描失败 [%s]: %s", scanner.get("name", "?"), e)
        return None
