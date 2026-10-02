# Dockerfile — webterm.py trên Alpine, chạy root để apk add được
FROM alpine:3.20

# Alpine mặc định chạy root → apk add OK, không cần sudo
RUN apk add --no-cache \
        python3 \
        bash \
        ca-certificates \
        curl \
        xvfb \
        x11vnc \
        icewm \
        xterm \
        ttf-dejavu \
        fontconfig \
    # Dọn cache & file rác để giảm size image và RAM footprint
    && rm -rf /var/cache/apk/* /tmp/* /var/tmp/*

WORKDIR /app
COPY webterm.py .

ENV PYTHONUNBUFFERED=1

# --vnc: bật Xvfb + x11vnc
# --wm icewm: WM siêu nhẹ (~5-15MB RAM) thay cho xfce4 (~150-300MB)
# Render tự inject $PORT → code tự bind 0.0.0.0 + allow_lan
CMD ["python3", "webterm.py", "--vnc", "--wm", "icewm"]
