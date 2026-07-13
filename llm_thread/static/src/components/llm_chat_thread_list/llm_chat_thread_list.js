/** @odoo-module **/

import { registerMessagingComponent } from "@mail/utils/messaging_component";
import { useModels } from "@mail/component_hooks/use_models";
import { useService } from "@web/core/utils/hooks";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { sprintf } from "@web/core/utils/strings";

const { Component, useState } = owl;

export class LLMChatThreadList extends Component {
  setup() {
    useModels();
    super.setup();
    this.dialog = useService("dialog");
    this.state = useState({
      isLoading: false,
    });
  }

  /**
   * @returns {LLMChatView}
   */
  get llmChatView() {
    return this.props.record;
  }

  /**
   * @returns {Thread}
   */
  get activeThread() {
    return this.llmChatView.llmChat.activeThread;
  }

  /**
   * @returns {boolean} True if the current user can delete threads
   */
  get isLLMManager() {
    return this.llmChatView.llmChat.isLLMManager;
  }

  /**
   * @returns {boolean} True if the sidebar is currently showing archived threads
   */
  get showArchived() {
    return this.llmChatView.llmChat.showArchived;
  }

  /**
   * Handle thread click
   * @param {Thread} thread
   */
  async _onThreadClick(thread) {
    if (this.state.isLoading) return;

    this.state.isLoading = true;
    try {
      await this.llmChatView.llmChat.selectThread(thread.id);
      this.llmChatView.update({
        isThreadListVisible: false,
      });
    } catch (error) {
      console.error("Error selecting thread:", error);
      this.messaging.notify({
        title: "Error",
        message: "Failed to load thread",
        type: "danger",
      });
    } finally {
      this.state.isLoading = false;
    }
  }

  /**
   * Handle thread delete
   * @param {Thread} thread
   */
  async _onThreadDelete(thread) {
    const confirmed = await new Promise((resolve) => {
      this.dialog.add(
        ConfirmationDialog,
        {
          title: this.env._t("Delete Conversation"),
          body: sprintf(
            this.env._t('Delete "%s"? This cannot be undone.'),
            thread.name
          ),
          confirm: () => resolve(true),
          cancel: () => resolve(false),
        },
        { onClose: () => resolve(false) }
      );
    });

    if (!confirmed) {
      return;
    }

    try {
      await this.llmChatView.llmChat.deleteThread(thread.id);
      this.messaging.notify({
        title: this.env._t("Deleted"),
        message: this.env._t("Conversation deleted"),
        type: "success",
      });
    } catch (error) {
      console.error("Error deleting thread:", error);
      this.messaging.notify({
        title: this.env._t("Error"),
        message: this.env._t("Failed to delete conversation"),
        type: "danger",
      });
    }
  }

  /**
   * Handle thread archive (set active=false)
   * @param {Thread} thread
   */
  async _onThreadArchive(thread) {
    try {
      await this.llmChatView.llmChat.archiveThread(thread.id);
      this.messaging.notify({
        title: this.env._t("Archived"),
        message: this.env._t("Conversation archived"),
        type: "success",
      });
    } catch (error) {
      console.error("Error archiving thread:", error);
      this.messaging.notify({
        title: this.env._t("Error"),
        message: this.env._t("Failed to archive conversation"),
        type: "danger",
      });
    }
  }

  /**
   * Handle thread unarchive (set active=true)
   * @param {Thread} thread
   */
  async _onThreadUnarchive(thread) {
    try {
      await this.llmChatView.llmChat.unarchiveThread(thread.id);
      this.messaging.notify({
        title: this.env._t("Restored"),
        message: this.env._t("Conversation restored"),
        type: "success",
      });
    } catch (error) {
      console.error("Error unarchiving thread:", error);
      this.messaging.notify({
        title: this.env._t("Error"),
        message: this.env._t("Failed to restore conversation"),
        type: "danger",
      });
    }
  }
}

Object.assign(LLMChatThreadList, {
  props: { record: Object },
  template: "llm_thread.LLMChatThreadList",
});

registerMessagingComponent(LLMChatThreadList);
