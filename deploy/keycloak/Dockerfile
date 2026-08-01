# Keycloak with a header limit that survives a polluted localhost cookie jar.
# Everything else is stock; the only change is a build-time Quarkus property.
FROM quay.io/keycloak/keycloak:26.6.3 AS builder
COPY quarkus.properties /opt/keycloak/conf/quarkus.properties
RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.6.3
COPY --from=builder /opt/keycloak/ /opt/keycloak/
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
