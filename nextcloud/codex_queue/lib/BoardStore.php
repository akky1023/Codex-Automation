<?php

declare(strict_types=1);

namespace OCA\CodexQueue;

use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\IDBConnection;

final class BoardStore {
    public function __construct(private IDBConnection $db) {}

    public function snapshot(string $owner): array {
        $query = $this->db->getQueryBuilder();
        $query->select('snapshot')->from('cq_boards')
            ->where($query->expr()->eq('owner', $query->createNamedParameter($owner)));
        $value = $query->executeQuery()->fetchOne();
        return $value === false ? [] : json_decode($value, true, flags: JSON_THROW_ON_ERROR);
    }

    public function requests(string $owner, bool $pending): array {
        $query = $this->db->getQueryBuilder();
        $query->select('id', 'kind', 'target_id', 'title', 'body', 'session_id', 'round_no', 'branch_mode', 'outcome')->from('cq_requests')
            ->where($query->expr()->eq('owner', $query->createNamedParameter($owner)));
        if ($pending) {
            $query->andWhere($query->expr()->isNull('outcome'))->orderBy('id', 'ASC')->setMaxResults(20);
        } else {
            $query->orderBy('id', 'DESC')->setMaxResults(20);
        }
        return $query->executeQuery()->fetchAllAssociative();
    }

    public function submit(string $owner, string $kind, int $target, string $title, string $body,
        ?string $sessionId = null, ?int $round = null, ?string $branchMode = null): void {
        $query = $this->db->getQueryBuilder();
        $query->insert('cq_requests')->values([
            'owner' => $query->createNamedParameter($owner),
            'kind' => $query->createNamedParameter($kind),
            'target_id' => $query->createNamedParameter($target, IQueryBuilder::PARAM_INT),
            'title' => $query->createNamedParameter($title),
            'body' => $query->createNamedParameter($body),
            'session_id' => $query->createNamedParameter($sessionId),
            'round_no' => $query->createNamedParameter($round, $round === null ? IQueryBuilder::PARAM_NULL : IQueryBuilder::PARAM_INT),
            'branch_mode' => $query->createNamedParameter($branchMode),
        ])->executeStatement();
    }

    public function publish(string $owner, array $snapshot, array $results): void {
        $json = json_encode($snapshot, JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE);
        $this->db->beginTransaction();
        try {
            $this->db->insertIgnoreConflict('*PREFIX*cq_boards', ['owner' => $owner, 'snapshot' => '{}']);
            $query = $this->db->getQueryBuilder();
            $query->update('cq_boards')->set('snapshot', $query->createNamedParameter($json))
                ->where($query->expr()->eq('owner', $query->createNamedParameter($owner)))
                ->executeStatement();
            foreach ($results as $result) {
                $query = $this->db->getQueryBuilder();
                $query->update('cq_requests')
                    ->set('outcome', $query->createNamedParameter($result['outcome']))
                    ->where($query->expr()->eq('owner', $query->createNamedParameter($owner)))
                    ->andWhere($query->expr()->eq('id', $query->createNamedParameter($result['id'], IQueryBuilder::PARAM_INT)))
                    ->andWhere($query->expr()->isNull('outcome'))->executeStatement();
            }
            $this->db->commit();
        } catch (\Throwable $error) {
            $this->db->rollBack();
            throw $error;
        }
    }
}
