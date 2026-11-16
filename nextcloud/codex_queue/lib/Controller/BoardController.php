<?php

declare(strict_types=1);

namespace OCA\CodexQueue\Controller;

use OCA\CodexQueue\BoardStore;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\DataResponse;
use OCP\AppFramework\OCSController;
use OCP\IRequest;
use OCP\IUserSession;

final class BoardController extends OCSController {
    public function __construct(IRequest $request, private IUserSession $session, private BoardStore $board) {
        parent::__construct('codex_queue', $request);
    }

    #[NoAdminRequired]
    public function read(): DataResponse {
        $owner = $this->session->getUser()->getUID();
        return new DataResponse($this->state($owner));
    }

    private function state(string $owner): array {
        // Read the snapshot between two pending reads to avoid showing an Idea twice
        // if the bridge publishes its result while the board is being read.
        do {
            $before = $this->board->pending($owner);
            $snapshot = $this->board->snapshot($owner);
            $pending = $this->board->pending($owner);
        } while ($before !== $pending);
        return ['snapshot' => $snapshot, 'pending' => $pending, 'requests' => $this->board->history($owner)];
    }

    #[NoAdminRequired]
    public function request(string $kind, int $target = 0, string $title = '', string $body = '', int $cancelId = 0): DataResponse {
        $owner = $this->session->getUser()->getUID();
        if ($cancelId > 0 && in_array($kind, ['remove', 'plan', 'revise', 'reply', 'retry', 'done', 'abort', 'sync'], true)) {
            if (!$this->board->cancel($owner, $cancelId, $kind, $target)) {
                return new DataResponse(['message' => 'この予約はすでに同期処理に渡されたか、取り消されています。'], 409);
            }
            return new DataResponse($this->state($owner) + ['message' => '予約を取り消しました。']);
        }
        $title = trim($title);
        $body = trim($body);
        $valid = match ($kind) {
            'add' => $target > 0 && $title !== '' && !preg_match('/\R/u', $title),
            'edit' => $target > 0 && $title !== '' && !preg_match('/\R/u', $title),
            'remove', 'plan' => $target > 0,
            'revise', 'reply' => $target > 0 && $body !== '',
            'retry', 'done', 'abort' => $target > 0,
            'sync' => $target === 0,
            default => false,
        };
        if (!$valid || mb_strlen($title) > 200 || mb_strlen($body) > 12000) {
            return new DataResponse(['message' => '入力内容を確認してください。'], 400);
        }
        $this->board->submit($owner, $kind, $target, $title, $body);
        return new DataResponse($this->state($owner) + ['message' => '依頼を受け付けました。'], 202);
    }
}
