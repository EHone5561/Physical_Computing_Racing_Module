"""
qr_read.py — 웹캠 프레임에서 QR을 읽는다. 하드웨어 없이 시험할 수 있게 hw.py와 분리했다.

OpenCV 기본 검출기(detectAndDecodeMulti) 하나만 쓰면 화면 반사로 대비가 낮거나 조금 흐린 프레임에서
QR이 화면 안에 있어도 자주 놓친다. 그래서 여러 방법을 순서대로 시도하고, 하나라도 읽으면 바로 돌려준다.
  1) pyzbar(zbar)가 설치돼 있으면 먼저 쓴다 (가장 잘 읽는다: sudo apt install libzbar0 && pip install pyzbar)
  2) 흑백 → 대비 보정(CLAHE) → 선명하게 → 흑백 이진화 → 확대 순으로 이미지를 바꿔 가며
     OpenCV 검출기 두 종류(Aruco 기반, 기본)로 읽는다
시간 제한(budget_s)을 두어, QR이 없는 프레임에서 너무 오래 붙잡지 않게 한다.
"""
import time

import cv2

try:
    from pyzbar import pyzbar
except Exception:   # 설치 안 됨 / libzbar 없음
    pyzbar = None

_detectors = None   # 처음 읽을 때 만든다 (import만 하는 테스트에서는 만들지 않는다)
_clahe = None


def _setup():
    global _detectors, _clahe
    if _detectors is None:
        _detectors = [cv2.QRCodeDetector()]
        if hasattr(cv2, "QRCodeDetectorAruco"):
            _detectors.insert(0, cv2.QRCodeDetectorAruco())
        _clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))


def _variants(gray):
    """(이미지, 원본 대비 배율)을 효과가 큰 순서로 낸다"""
    yield gray, 1.0
    eq = _clahe.apply(gray)
    yield eq, 1.0
    blur = cv2.GaussianBlur(eq, (0, 0), 2)
    sharp = cv2.addWeighted(eq, 1.8, blur, -0.8, 0)
    yield sharp, 1.0
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield binary, 1.0
    if max(gray.shape) <= 1280:
        yield cv2.resize(sharp, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC), 1.6


def _opencv(img, scale):
    for det in _detectors:
        ok, texts, points, _ = det.detectAndDecodeMulti(img)
        if ok and points is not None:
            found = [(t.strip(), float(p[:, 0].mean()) / scale) for t, p in zip(texts, points) if t.strip()]
            if found:
                return found
        text, pts, _ = det.detectAndDecode(img)
        if text and pts is not None:
            return [(text.strip(), float(pts.reshape(-1, 2)[:, 0].mean()) / scale)]
    return []


def read_qrs(frame, budget_s=0.25):
    """frame 안의 QR들을 [(문자열, 중심 x좌표), ...]로 돌려준다. 없으면 []."""
    _setup()
    gray =frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if pyzbar is not None:
        found = [(c.data.decode("utf-8", "ignore").strip(), c.rect.left + c.rect.width / 2)
                 for c in pyzbar.decode(gray) if c.type == "QRCODE"]
        if found:
            return found
    deadline = time.monotonic() + budget_s
    for img, scale in _variants(gray):
        found = _opencv(img, scale)
        if found:
            return found
        if time.monotonic() > deadline:
            break
    return []
