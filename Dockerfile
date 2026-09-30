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
        openbox \
        xterm \
        xfce4 \
        ttf-dejavu \
        fontconfig

WORKDIR /app
COPY webterm.py .

ENV PYTHONUNBUFFERED=1

# --vnc: bật Xvfb + x11vnc
# --wm openbox: WM nhẹ, đủ để có desktop
# Render tự inject $PORT → code tự bind 0.0.0.0 + allow_lan
CMD ["python3", "webterm.py", "--vnc", "--wm", "openbox"]
