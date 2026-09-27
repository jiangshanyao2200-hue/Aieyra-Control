'use strict';
const { CATEGORIES } = require('./human-notifications.cjs');
class HumanNotificationHost {
  constructor({
    ledger,
    Notification,
    icon,
    open,
    focused = () => false,
    changed = () => {},
    aggregateMs = 2000,
  }) {
    Object.assign(this, { ledger, Notification, icon, open, focused, changed, aggregateMs });
    this.timer = null;
    this.toasts = new Set();
    this.stopped = true;
    this.onChange = () => {
      this.changed();
      this.schedule();
    };
  }
  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.ledger.on('change', this.onChange);
    this.schedule();
  }
  schedule() {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.stopped || this.focused() || !this.Notification.isSupported()) return;
    const delay = this.ledger.nextAttentionDelay();
    if (delay === null) return;
    this.timer = setTimeout(
      () => {
        this.timer = null;
        this.present();
        this.schedule();
      },
      Math.max(delay, this.aggregateMs),
    );
    this.timer.unref?.();
  }
  present() {
    if (this.stopped || this.focused() || !this.Notification.isSupported()) return false;
    const batch = this.ledger.reserve();
    if (!batch) return false;
    try {
      // Only category/count leave the Control surface. No project, question, key or task text.
      const toast = new this.Notification({
        title: `Aieyra Control · ${batch.total} 项需要你处理`,
        body: `${batch.categories.join(' · ')}\n点击前往指挥中心查看，任务会继续等待你的决定。`,
        icon: this.icon,
        silent: true,
        urgency: 'normal',
        timeoutType: 'default',
      });
      this.toasts.add(toast);
      toast.once('show', () => this.ledger.presentation(batch, 'shown'));
      toast.once('failed', () => {
        this.ledger.presentation(batch, 'failed');
        this.toasts.delete(toast);
      });
      toast.once('close', () => this.toasts.delete(toast));
      toast.once('click', () => {
        this.toasts.delete(toast);
        // Only navigation. The real page will GET the latest version before any decision.
        this.open(batch.ids[0]?.id || null);
      });
      toast.show();
      this.changed();
      return true;
    } catch {
      this.ledger.presentation(batch, 'failed');
      this.changed();
      return false;
    }
  }
  menu() {
    const state = this.ledger.snapshot(),
      suffix = state.current ? '' : '（等待同步）';
    const detail = Object.entries(state.categories)
      .filter(([, n]) => n)
      .map(([category, n]) => `${CATEGORIES[category]} ${n}`)
      .join(' · ');
    return [
      { label: `需要你处理 · ${state.pending_count}${suffix}`, click: () => this.open(null) },
      ...(detail ? [{ label: detail, enabled: false }] : []),
      {
        label: state.persistence_error
          ? '提醒记录无法读取，请在指挥中心查看'
          : state.snoozed_until > Date.now()
            ? '桌面提醒已延后'
            : '人工事项桌面提醒',
        type: 'checkbox',
        checked: state.enabled,
        enabled: !state.persistence_error,
        click: (item) => this.ledger.setEnabled(item.checked),
      },
      {
        label: '延后提醒',
        enabled: !state.persistence_error,
        submenu: [
          { label: '15 分钟', click: () => this.ledger.snooze(15) },
          { label: '1 小时', click: () => this.ledger.snooze(60) },
          {
            label: '恢复提醒',
            enabled: state.snoozed_until > Date.now(),
            click: () => this.ledger.snooze(0),
          },
        ],
      },
    ];
  }
  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.timer = null;
    this.ledger.off('change', this.onChange);
    for (const toast of this.toasts) {
      try {
        toast.close();
      } catch {}
    }
    this.toasts.clear();
  }
}
module.exports = { HumanNotificationHost };
