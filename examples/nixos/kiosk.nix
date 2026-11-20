# NixOS kiosk: Cage + Firefox full screen showing the dashboard on the built-in display.
# Import it from /etc/nixos/configuration.nix:  imports = [ ./kiosk.nix ];
{ pkgs, ... }:

let
  url = "http://localhost:8090";
  backlight = value: ''
    for f in /sys/class/backlight/*/bl_power; do echo ${value} > "$f"; done
  '';
in
{
  networking.firewall.allowedTCPPorts = [ 8090 ];

  # Laptop as a server: closing the lid does not suspend
  services.logind.settings.Login = {
    HandleLidSwitch = "ignore";
    HandleLidSwitchExternalPower = "ignore";
    HandleLidSwitchDocked = "ignore";
  };

  # Unprivileged kiosk user (no sudo, no docker)
  users.users.kiosk = { isNormalUser = true; description = "Kiosk"; };

  programs.firefox = {
    enable = true;
    policies = {
      DontCheckDefaultBrowser = true;
      OverrideFirstRunPage = "";
      OverridePostUpdatePage = "";
      DisableTelemetry = true;
      DisableFirefoxStudies = true;
      NoDefaultBookmarks = true;
      PasswordManagerEnabled = false;
      TranslateEnabled = false;
    };
    preferences."browser.sessionstore.resume_from_crash" = false;
  };

  services.cage = {
    enable = true;
    user = "kiosk";
    program = "/run/current-system/sw/bin/firefox --kiosk ${url}";
  };

  # tty1 belongs to the kiosk, so a nixos-rebuild never replaces it with a login prompt
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;

  # Wait for the dashboard before starting Firefox; restart if it ever exits
  systemd.services."cage-tty1" = {
    after = [ "docker.service" ];
    wants = [ "docker.service" ];
    serviceConfig = {
      ExecStartPre = "${pkgs.bash}/bin/bash -c 'until ${pkgs.curl}/bin/curl -sf -o /dev/null ${url}/status.json; do sleep 2; done'";
      TimeoutStartSec = "5min";
      Restart = "always";
      RestartSec = 5;
    };
  };

  # Screen off at 23:00, on at 07:00 (bl_power: 0 = on, 4 = off)
  systemd.services.screen-off = { serviceConfig.Type = "oneshot"; script = backlight "4"; };
  systemd.services.screen-on  = { serviceConfig.Type = "oneshot"; script = backlight "0"; };
  systemd.timers.screen-off = { wantedBy = [ "timers.target" ]; timerConfig.OnCalendar = "*-*-* 23:00:00"; };
  systemd.timers.screen-on  = { wantedBy = [ "timers.target" ]; timerConfig.OnCalendar = "*-*-* 07:00:00"; };
}
