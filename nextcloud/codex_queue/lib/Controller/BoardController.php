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
        return new DataResponse([
            'snapshot' => $this->board->snapshot($owner),
            'requests' => $this->board->requests($owner, false),
        ]);
    }

    #[NoAdminRequired]
    public function request(string $kind, int $target = 0, string $title = '', string $body = ''): DataResponse {
        $title = trim($title);
        $body = trim($body);
        $valid = match ($kind) {
            'add' => $target > 0 && $title !== '' && !preg_match('/\R/u', $title),
            'edit' => $target > 0 && $title !== '' && !preg_match('/\R/u', $title),
            'remove', 'plan' => $target > 0,
            'revise', 'reply' => $target > 0 && $body !== '',
            'retry' => $target > 0,
            'sync' => $target === 0,
            default => false,
        };
        if (!$valid || mb_strlen($title) > 200 || mb_strlen($body) > 12000) {
            return new DataResponse(['message' => '入力内容を確認してください。'], 400);
        }
        $this->board->submit($this->session->getUser()->getUID(), $kind, $target, $title, $body);
        return new DataResponse(['message' => '依頼を受け付けました。'], 202);
    }
}
