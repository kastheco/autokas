FROM wiziscool/oh-my-cpa:v0.2.9

USER root
# Render SSH needs a login shell and a writable home for the application user.
RUN apk add --no-cache sqlite \
    && sed -i 's|^\(omc:x:10001:10001:[^:]*:/home/omc:\)/sbin/nologin$|\1/bin/sh|' /etc/passwd \
    && test "$(awk -F: '$1 == "omc" { print $7 }' /etc/passwd)" = /bin/sh \
    && mkdir -p /home/omc/.ssh \
    && chown -R 10001:10001 /home/omc \
    && chmod 700 /home/omc/.ssh

USER 10001:10001
