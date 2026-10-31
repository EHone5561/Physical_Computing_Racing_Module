"""
qr_read.py — 웹캠 프레임에서 QR을 읽는다. 하드웨어 없이 시험할 수 있게 hw.py와 분리했다.

OpenCV 기본 검출기(detectAndDecodeMulti) 하나만 쓰면 화면 반사로 대비가 낮거나 조금 흐린 프레임에서
QR이 화면 안에 있어도 자주 놓친다. 그래서 여러 방법을 순서대로 시도하고, 하나라도 읽으면 바로 돌려준다.
  1) pyzbar(zbar)가 설치돼 있으면 먼저 쓴다 (가장 잘 읽는다: sudo apt install libzbar0 && pip install pyzbar)
  2) expect_px(예상 QR 한 변 픽셀)를 알면, 화면 가운데를 잘라 QR이 TARGET_PX 크기가 되도록
     확대·축소한 뒤 먼저 읽는다. 출발선에서는 QR이 멀어 작게 찍히므로 이 단계가 가장 잘 읽고 빠르다
  3) 그래도 못 읽으면 전체 프레임을 흑백 → 대비 보정(CLAHE) → 선명하게 → 이진화 → 확대 순으로
     바꿔 가며 OpenCV 검출기 두 종류(Aruco 기반, 기본)로 읽는다
시간 제한(budget_s)을 두어, QR이 없는 프레임에서 너무 오래 붙잡지 않게 한다.
"""
import time

import cv2

try:
    from pyzbar import pyzbar
except Exception:   # 설치 안 됨 / libzbar 없음
    pyzbar = None

TARGET_PX = 160     # QR 한 변이 이 정도(모듈당 약 5~6px)일 때 OpenCV 검출기가 가장 잘 읽는다
_detectors = None   # 처음 읽을 때 만든다 (import만 하는 테스트에서는 만들지 않는다)
_clahe = None


def _setup():
    global _detectors, _clahe
    if _detectors is None:
        _detectors = [cv2.QRCodeDetector()]
        if hasattr(cv2, "QRCodeDetectorAruco"):
            _detectors.insert(0, cv2.QRCodeDetectorAruco())
        _clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))


def _sharpen(img, amount=1.8, sigma=2):
    blur = cv2.GaussianBlur(img, (0, 0), sigma)
    return cv2.addWeighted(img, amount, blur, 1 - amount, 0)


def _variants(gray):
    """전체 프레임용: (이미지, 원본 대비 배율)을 효과가 큰 순서로 낸다"""
    yield gray, 1.0
    eq = _clahe.apply(gray)
    yield eq, 1.0
    sharp = _sharpen(eq)
    yield sharp, 1.0
    _, binary = cv2.threshold(cv2.GaussianBlur(eq, (0, 0), 2), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield binary, 1.0
    if max(gray.shape) <= 1280:
        yield cv2.resize(sharp, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC), 1.6


def _center_roi(gray, expect_px, boost=1.0):
    """QR이 있을 화면 가운데를 잘라 QR이 TARGET_PX가 되도록 배율을 맞춘다. (이미지, 왼쪽 x, 배율)
    boost > 1이면 예상보다 작게 찍힌 QR을 위해 더 키운다"""
    H, W = gray.shape
    w = int(min(W, max(TARGET_PX, expect_px * 6)))   # 가운데에서 QR 2.5개 폭만큼 좌우로 어긋나도 들어오게
    h = int(min(H, max(TARGET_PX, expect_px * 4)))
    x0, y0 = (W - w) // 2, (H - h) // 2
    roi = gray[y0:y0 + h, x0:x0 + w]
    scale = min(4.0, max(0.5, TARGET_PX / expect_px * boost))
    if abs(scale - 1) < 0.15:
        return roi, x0, 1.0
    inter = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(roi, None, fx=scale, fy=scale, interpolation=inter), x0, scale


def _opencv(img, scale, x0=0):
    """(문자열, 원본 기준 중심 x, 원본 기준 한 변 픽셀) 목록"""
    def to_frame(pts):
        xs = pts.reshape(-1, 2)[:, 0]
        return x0 + float(xs.mean()) / scale, float(xs.max() - xs.min()) / scale
    for det in _detectors:
        ok, texts, points, _ = det.detectAndDecodeMulti(img)
        if ok and points is not None:
            found = [(t.strip(), *to_frame(p)) for t, p in zip(texts, points) if t.strip()]
            if found:
                return found
        text, pts, _ = det.detectAndDecode(img)
        if text and pts is not None:
            return [(text.strip(), *to_frame(pts))]
    return []


def _zbar(img, scale=1.0, x0=0):
    if pyzbar is None:
        return []
    return [(c.data.decode("utf-8", "ignore").strip(), x0 + (c.rect.left + c.rect.width / 2) / scale,
             c.rect.width / scale) for c in pyzbar.decode(img) if c.type == "QRCODE"]


def _opencv_single(img, scale, x0):
    """출발 QR은 하나뿐이라 한 개만 찾는 빠른 검출(detectAndDecode)만 쓴다.
    Aruco 기반 검출기는 흐린 실제 사진에서 거의 도움이 안 되고 시간만 들어서 기본 검출기만 쓴다"""
    for det in _detectors[-1:]:
        text, pts, _ = det.detectAndDecode(img)
        if text and pts is not None:
            xs = pts.reshape(-1, 2)[:, 0]
            return [(text.strip(), x0 + float(xs.mean()) / scale, float(xs.max() - xs.min()) / scale)]
    return []


def read_qrs_detail(frame, budget_s=0.25, expect_px=None):
    """[(문자열, 중심 x좌표, QR 한 변 픽셀), ...]. 없으면 []
    expect_px를 알면 화면 가운데만 잘라 읽어서 전체 프레임보다 훨씬 빠르다 (라즈베리파이에서 중요).
    전체 프레임은 시간이 남을 때만 마지막으로 본다"""
    _setup()
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    deadline = time.monotonic() + budget_s
    if not expect_px:
        found = _zbar(gray)
        if found:
            return found
    if expect_px:
        # 예상 크기는 거리·화각 오차로 틀릴 수 있어 배율을 세 번 바꿔 본다:
        # 1) 예상 그대로  2) 실제가 더 크게 찍힌 경우(가까움)  3) 더 작게 찍힌 경우(멂, 작은 QR)
        for boost in (1.0, 0.6, 1.7):
            roi, x0, scale = _center_roi(gray, expect_px, boost)
            found = _zbar(roi, scale, x0)
            if found:
                return found
            # 실제 웹캠 사진(초점이 흐림)에서는 강하게 선명하게 한 것과 주변 밝기 기준 이진화가 잘 읽혔고,
            # 원본·대비 보정만으로는 대부분 못 읽었다 (2026-10-31 qr_view.jpg 시험). 그래서 그 순서로 본다
            eq = _clahe.apply(roi)
            binary = cv2.adaptiveThreshold(cv2.GaussianBlur(eq, (0, 0), 1), 255,
                                           cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
            for img in (_sharpen(eq, 2.5, 3), binary, roi):
                found = _opencv_single(img, scale, x0)
                if found:
                    return found
                if time.monotonic() > deadline:
                    return []
    if expect_px and time.monotonic() < deadline:
        found = _zbar(gray)
        if found:
            return found
    for img, scale in _variants(gray):
        found = _opencv(img, scale)
        if found:
            return found
        if time.monotonic() > deadline:
            break
    return []


def read_qrs(frame, budget_s=0.25, expect_px=None):
    """frame 안의 QR들을 [(문자열, 중심 x좌표), ...]로 돌려준다. 없으면 []."""
    return [(t, cx) for t, cx, _ in read_qrs_detail(frame, budget_s, expect_px)]
