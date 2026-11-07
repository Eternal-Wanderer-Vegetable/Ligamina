FROM node:24.20.0-bookworm-slim AS profile
USER 0:0
WORKDIR /opt/ligamina/p405-koishi-profile
COPY package.json package-lock.json ./
COPY npm-cache.tar.gz /tmp/p405-npm-cache.tar.gz
COPY npm-cache-supplement.tar.gz /tmp/p405-npm-cache-supplement.tar.gz
COPY plugin-echo-2.2.5.tgz /tmp/plugin-echo-2.2.5.tgz
RUN mkdir -p /tmp/p405-npm && tar -xzf /tmp/p405-npm-cache.tar.gz -C /tmp/p405-npm && tar -xzf /tmp/p405-npm-cache-supplement.tar.gz -C /tmp/p405-npm && npm ci --ignore-scripts --strict-peer-deps --engine-strict --offline --cache /tmp/p405-npm/npm-cache --audit=false --fund=false && mkdir -p node_modules/@koishijs/plugin-echo && tar -xzf /tmp/plugin-echo-2.2.5.tgz -C node_modules/@koishijs/plugin-echo --strip-components=1 && node -e "for (const p of ['koishi','@koishijs/core','@koishijs/plugin-echo']) console.log(p + '=' + require('./node_modules/' + p + '/package.json').version)" && rm -rf /tmp/p405-npm /tmp/p405-npm-cache.tar.gz /tmp/p405-npm-cache-supplement.tar.gz /tmp/plugin-echo-2.2.5.tgz
FROM ligamina-p401-runtime-offline-profile:phase4-host-lock-20261026
USER 0:0
COPY --from=profile /opt/ligamina/p405-koishi-profile/node_modules /opt/ligamina/p405-koishi-profile/node_modules
USER 10001:10001
