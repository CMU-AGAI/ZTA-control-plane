FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY *.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .

# Default env vars — override per-component in docker-compose
ENV LISTEN_PORT=10000
ENV UPSTREAM_URL=http://localhost:8091
ENV COMPONENT_NAME=zta-sidecar
ENV OPA_URL=http://opa:8181
ENV JWT_ISSUER=zta-auth-server
ENV JWT_AUDIENCE=zta-agents
ENV JWT_SECRET=zta-dev-secret-key-change-in-production-min-32-chars!!
ENV ENABLE_WAF=true
ENV ENABLE_DLP=true
ENV OPA_FAIL_OPEN=false

EXPOSE 10000

ENTRYPOINT ["dotnet", "ZtaSidecar.dll"]
