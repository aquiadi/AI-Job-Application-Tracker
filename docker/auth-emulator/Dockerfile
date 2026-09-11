# The Firebase Auth emulator ships inside firebase-tools and has no official
# container image. Rather than depend on a community image, install it on top of the
# official Node image and pin the version.
FROM node:22-alpine

ARG FIREBASE_TOOLS_VERSION=15.30.0

RUN apk add --no-cache openjdk17-jre-headless curl \
    && npm install -g "firebase-tools@${FIREBASE_TOOLS_VERSION}" \
    && npm cache clean --force

WORKDIR /srv
COPY firebase.json ./

EXPOSE 4000 9099
