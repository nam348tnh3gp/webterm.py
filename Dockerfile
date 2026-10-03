# Dockerfile — webterm.py trên Arch Linux, chạy root để pacman add được
FROM archlinux:latest

# Arch mặc định chạy root → pacman -S OK, không cần sudo
# --noconfirm: tự động chấp nhận cài đặt
# -Scc: dọn sạch cache package sau khi cài để giảm size image
RUN pacman -Sy --noconfirm --needed \
        python \
        bash \
        ca-certificates \
        curl \
        xorg-server-xvfb \
        x11vnc \
        icewm \
        xterm \
        ttf-dejavu \
        fontconfig \
    && pacman -Scc --noconfirm \
    && rm -rf /tmp/* /var/tmp/*

WORKDIR /app
COPY webterm.py .

ENV PYTHONUNBUFFERED=1

# --vnc: bật Xvfb + x11vnc
# --wm icewm: WM siêu nhẹ (~5-15MB RAM)
# Render tự inject $PORT → code tự bind 0.0.0.0 + allow_lan
CMD ["python3", "webterm.py", "--vnc", "--wm", "icewm"]
