# Digest-pinned (#581): `:latest` is a moving tag invisible to
# tag-based update checks — Dependabot proposes digest updates weekly.
FROM tailscale/tailscale:latest@sha256:72c1e19e0aedca96b8533bbb0b4368564410ff20b17e53b804eb6c1f04a31be4

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
